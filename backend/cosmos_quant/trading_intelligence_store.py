from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from .storage import SqliteStore
from .trading_intelligence import (
    ActiveThesis,
    AutomationRuntimeStatus,
    AutomationRuntimeV2,
    DecisionEpisode,
    HumanLiveGrant,
    MarketStateV2,
    PlanState,
    PositionRuntimeV2,
    PositionStateV2,
    QuantPrediction,
    ThesisState,
    TradePlan,
    WaitRuntimeStatus,
    WaitRuntimeV2,
    content_hash,
    require_transition,
    validate_market_state_time,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


def _json(model: BaseModel | dict[str, Any]) -> str:
    if isinstance(model, BaseModel):
        return model.model_dump_json(exclude_none=True)
    return json.dumps(model, ensure_ascii=False, separators=(",", ":"), default=str)


class TradingIntelligenceStore:
    """Transactional v2 state, event ledger, and exactly-once wake persistence."""

    def __init__(self, store: SqliteStore):
        self.store = store

    def save_market_state(self, state: MarketStateV2) -> bool:
        validate_market_state_time(state, max_age=None)
        with self.store._lock, self.store.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO ti_market_states_v2
                    (state_id, symbol, state_hash, data_as_of, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    state.state_id,
                    state.symbol,
                    state.state_hash,
                    state.data_as_of.isoformat(),
                    state.model_dump_json(
                        exclude_none=True,
                        exclude_computed_fields=True,
                    ),
                    state.created_at.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def latest_market_state(self, symbol: str) -> MarketStateV2 | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM ti_market_states_v2
                WHERE symbol = ? AND data_as_of <= ? ORDER BY data_as_of DESC LIMIT 1
                """,
                (symbol, (datetime.now(UTC) + timedelta(seconds=30)).isoformat()),
            ).fetchone()
        return MarketStateV2.model_validate_json(row["payload"]) if row else None

    def previous_market_state(
        self,
        symbol: str,
        *,
        before_state_hash: str,
        before_data_as_of: datetime | None = None,
    ) -> MarketStateV2 | None:
        cutoff = (before_data_as_of or datetime.now(UTC)).isoformat()
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM ti_market_states_v2
                WHERE symbol = ? AND state_hash <> ? AND data_as_of <= ?
                ORDER BY data_as_of DESC LIMIT 1
                """,
                (symbol, before_state_hash, cutoff),
            ).fetchone()
        return MarketStateV2.model_validate_json(row["payload"]) if row else None

    def save_thesis(self, thesis: ActiveThesis) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT version FROM ti_theses_v2 WHERE thesis_id = ?", (thesis.thesis_id,)
            ).fetchone()
            if existing and int(existing["version"]) > thesis.version:
                raise RuntimeError("stale thesis version")
            connection.execute(
                """
                INSERT INTO ti_theses_v2
                    (thesis_id, version, symbol, state, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thesis_id) DO UPDATE SET
                    version = excluded.version,
                    state = excluded.state,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                WHERE ti_theses_v2.version <= excluded.version
                """,
                (
                    thesis.thesis_id,
                    thesis.version,
                    thesis.symbol,
                    str(thesis.state),
                    thesis.model_dump_json(exclude_none=True),
                    thesis.created_at.isoformat(),
                    now,
                ),
            )

    def transition_thesis(
        self, thesis_id: str, *, expected_version: int, target: ThesisState
    ) -> ActiveThesis:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_theses_v2 WHERE thesis_id = ?", (thesis_id,)
            ).fetchone()
            if not row:
                raise KeyError(thesis_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent thesis update")
            thesis = ActiveThesis.model_validate_json(row["payload"])
            require_transition(ThesisState(thesis.state), target)
            updated = thesis.model_copy(update={"state": target, "version": expected_version + 1})
            cursor = connection.execute(
                """
                UPDATE ti_theses_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE thesis_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(target),
                    updated.model_dump_json(exclude_none=True),
                    datetime.now(UTC).isoformat(),
                    thesis_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent thesis update")
        return updated

    def update_thesis_content(self, thesis: ActiveThesis, *, expected_version: int) -> ActiveThesis:
        """Update an active thesis using server-owned optimistic versioning."""

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_theses_v2 WHERE thesis_id = ?",
                (thesis.thesis_id,),
            ).fetchone()
            if not row:
                raise KeyError(thesis.thesis_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent thesis update")
            current = ActiveThesis.model_validate_json(row["payload"])
            updated = thesis.model_copy(
                update={
                    "version": expected_version + 1,
                    "state": current.state,
                    "created_at": current.created_at,
                }
            )
            cursor = connection.execute(
                """
                UPDATE ti_theses_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE thesis_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(updated.state),
                    updated.model_dump_json(exclude_none=True),
                    datetime.now(UTC).isoformat(),
                    updated.thesis_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent thesis update")
        return updated

    def get_thesis(self, thesis_id: str) -> ActiveThesis | None:
        return self._get_model("ti_theses_v2", "thesis_id", thesis_id, ActiveThesis)

    def list_active_theses(self, symbol: str | None = None) -> list[ActiveThesis]:
        query = "SELECT payload FROM ti_theses_v2 WHERE state = ?"
        params: list[Any] = [str(ThesisState.ACTIVE)]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY updated_at DESC"
        return self._list_models(query, params, ActiveThesis)

    def cancel_other_active_theses(self, symbol: str, *, keep_thesis_id: str) -> list[ActiveThesis]:
        cancelled: list[ActiveThesis] = []
        for thesis in self.list_active_theses(symbol):
            if thesis.thesis_id == keep_thesis_id:
                continue
            cancelled.append(
                self.transition_thesis(
                    thesis.thesis_id,
                    expected_version=thesis.version,
                    target=ThesisState.CANCELLED,
                )
            )
        return cancelled

    def save_plan(self, plan: TradePlan) -> None:
        if not self.get_thesis(plan.thesis_id):
            raise ValueError("trade plan references an unknown thesis")
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT version FROM ti_plans_v2 WHERE plan_id = ?", (plan.plan_id,)
            ).fetchone()
            if existing and int(existing["version"]) > plan.version:
                raise RuntimeError("stale plan version")
            connection.execute(
                """
                INSERT INTO ti_plans_v2
                    (plan_id, thesis_id, version, symbol, state, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    version = excluded.version,
                    state = excluded.state,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                WHERE ti_plans_v2.version <= excluded.version
                """,
                (
                    plan.plan_id,
                    plan.thesis_id,
                    plan.version,
                    plan.symbol,
                    str(plan.state),
                    plan.model_dump_json(exclude_none=True),
                    plan.created_at.isoformat(),
                    now,
                ),
            )

    def transition_plan(
        self, plan_id: str, *, expected_version: int, target: PlanState
    ) -> TradePlan:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_plans_v2 WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if not row:
                raise KeyError(plan_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent plan update")
            plan = TradePlan.model_validate_json(row["payload"])
            require_transition(PlanState(plan.state), target)
            updated = plan.model_copy(update={"state": target, "version": expected_version + 1})
            cursor = connection.execute(
                """
                UPDATE ti_plans_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE plan_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(target),
                    updated.model_dump_json(exclude_none=True),
                    datetime.now(UTC).isoformat(),
                    plan_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent plan update")
        return updated

    def get_plan(self, plan_id: str) -> TradePlan | None:
        return self._get_model("ti_plans_v2", "plan_id", plan_id, TradePlan)

    def update_plan_content(self, plan: TradePlan, *, expected_version: int) -> TradePlan:
        """Update deterministic management rules without changing lifecycle state."""

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_plans_v2 WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone()
            if not row:
                raise KeyError(plan.plan_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent plan update")
            current = TradePlan.model_validate_json(row["payload"])
            updated = plan.model_copy(
                update={
                    "version": expected_version + 1,
                    "state": current.state,
                    "created_at": current.created_at,
                }
            )
            cursor = connection.execute(
                """
                UPDATE ti_plans_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE plan_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(updated.state),
                    updated.model_dump_json(exclude_none=True),
                    datetime.now(UTC).isoformat(),
                    updated.plan_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent plan update")
        return updated

    def list_open_plans(self, symbol: str | None = None) -> list[TradePlan]:
        terminal = tuple(
            str(item)
            for item in (
                PlanState.COMPLETED,
                PlanState.EXPIRED,
                PlanState.INVALIDATED,
                PlanState.CANCELLED,
            )
        )
        placeholders = ",".join("?" for _ in terminal)
        query = f"SELECT payload FROM ti_plans_v2 WHERE state NOT IN ({placeholders})"
        params: list[Any] = list(terminal)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY updated_at DESC"
        return self._list_models(query, params, TradePlan)

    def cancel_open_plans(self, symbol: str, *, keep_plan_id: str | None = None) -> list[TradePlan]:
        cancelled: list[TradePlan] = []
        for plan in self.list_open_plans(symbol):
            if plan.plan_id == keep_plan_id or self.position_for_plan(plan.plan_id):
                continue
            cancelled.append(
                self.transition_plan(
                    plan.plan_id,
                    expected_version=plan.version,
                    target=PlanState.CANCELLED,
                )
            )
        return cancelled

    def save_position(self, position: PositionRuntimeV2) -> None:
        if not self.get_plan(position.plan_id):
            raise ValueError("position references an unknown v2 plan")
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT version FROM ti_positions_v2 WHERE position_id = ?",
                (position.position_id,),
            ).fetchone()
            if existing and int(existing["version"]) >= position.version:
                raise RuntimeError("stale or duplicate position version")
            connection.execute(
                """
                INSERT INTO ti_positions_v2
                    (position_id, plan_id, version, symbol, state, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    version = excluded.version,
                    state = excluded.state,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    position.position_id,
                    position.plan_id,
                    position.version,
                    position.symbol,
                    str(position.state),
                    position.model_dump_json(exclude_none=True),
                    position.opened_at.isoformat(),
                    now,
                ),
            )

    def recover_legacy_position_quantity_overflows(self) -> list[dict[str, Any]]:
        """Make pre-fix overflow snapshots readable while keeping trading halted.

        Older reconciliation code could copy a venue's aggregate position size
        into each logical position. ``model_copy`` skipped Pydantic validation,
        so an invalid row could then prevent the API from starting. Cap only
        that known overflow shape; callers must halt automated management and
        require account reconciliation before trading can resume.
        """

        recovered: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT position_id, version, payload
                FROM ti_positions_v2
                WHERE state NOT IN ('CLOSED', 'FLAT')
                """
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                original = Decimal(str(payload.get("original_quantity")))
                observed = Decimal(str(payload.get("remaining_quantity")))
                if observed <= original:
                    continue
                old_version = int(row["version"])
                payload.update(
                    remaining_quantity=str(original),
                    version=old_version + 1,
                    updated_at=now.isoformat(),
                )
                repaired = PositionRuntimeV2.model_validate(payload)
                connection.execute(
                    """
                    UPDATE ti_positions_v2
                    SET version = ?, state = ?, payload = ?, updated_at = ?
                    WHERE position_id = ? AND version = ?
                    """,
                    (
                        repaired.version,
                        str(repaired.state),
                        repaired.model_dump_json(exclude_none=True),
                        repaired.updated_at.isoformat(),
                        repaired.position_id,
                        old_version,
                    ),
                )
                recovered.append(
                    {
                        "position_id": repaired.position_id,
                        "version": repaired.version,
                        "original_quantity": str(original),
                        "observed_remaining_quantity": str(observed),
                    }
                )
        for item in recovered:
            self.append_event(
                dedupe_key=(
                    f"position-quantity-overflow-recovered:{item['position_id']}:{item['version']}"
                ),
                event_type="POSITION_QUANTITY_OVERFLOW_RECOVERED",
                aggregate_type="position",
                aggregate_id=item["position_id"],
                aggregate_version=item["version"],
                payload={
                    "original_quantity": item["original_quantity"],
                    "observed_remaining_quantity": item["observed_remaining_quantity"],
                    "automatic_management_halted": True,
                },
            )
        return recovered

    def transition_position(
        self,
        position_id: str,
        *,
        expected_version: int,
        target: PositionStateV2,
        updates: dict[str, Any] | None = None,
    ) -> PositionRuntimeV2:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_positions_v2 WHERE position_id = ?",
                (position_id,),
            ).fetchone()
            if not row:
                raise KeyError(position_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent position update")
            position = PositionRuntimeV2.model_validate_json(row["payload"])
            require_transition(PositionStateV2(position.state), target)
            payload_updates = {
                **(updates or {}),
                "state": target,
                "version": expected_version + 1,
                "updated_at": datetime.now(UTC),
            }
            updated = PositionRuntimeV2.model_validate(
                {**position.model_dump(mode="python"), **payload_updates}
            )
            cursor = connection.execute(
                """
                UPDATE ti_positions_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE position_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(target),
                    updated.model_dump_json(exclude_none=True),
                    updated.updated_at.isoformat(),
                    position_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent position update")
        return updated

    def update_position(
        self,
        position_id: str,
        *,
        expected_version: int,
        updates: dict[str, Any],
    ) -> PositionRuntimeV2:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, version FROM ti_positions_v2 WHERE position_id = ?",
                (position_id,),
            ).fetchone()
            if not row:
                raise KeyError(position_id)
            if int(row["version"]) != expected_version:
                raise RuntimeError("concurrent position update")
            position = PositionRuntimeV2.model_validate_json(row["payload"])
            updated = PositionRuntimeV2.model_validate(
                {
                    **position.model_dump(mode="python"),
                    **updates,
                    "version": expected_version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            cursor = connection.execute(
                """
                UPDATE ti_positions_v2
                SET version = ?, state = ?, payload = ?, updated_at = ?
                WHERE position_id = ? AND version = ?
                """,
                (
                    updated.version,
                    str(updated.state),
                    updated.model_dump_json(exclude_none=True),
                    updated.updated_at.isoformat(),
                    position_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("concurrent position update")
        return updated

    def get_position(self, position_id: str) -> PositionRuntimeV2 | None:
        return self._get_model("ti_positions_v2", "position_id", position_id, PositionRuntimeV2)

    def position_for_plan(self, plan_id: str) -> PositionRuntimeV2 | None:
        direct = self._get_model("ti_positions_v2", "plan_id", plan_id, PositionRuntimeV2)
        return direct or next(
            (p for p in self.list_positions(include_closed=True) if plan_id in p.plan_ids), None
        )

    def episode_for_plan(self, plan_id: str) -> DecisionEpisode | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM ti_decision_episodes_v2 "
                "WHERE json_extract(payload, '$.plan_id') = ? ORDER BY updated_at DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        return DecisionEpisode.model_validate_json(row["payload"]) if row else None

    def get_entry_order(self, entry_id: str):
        from .entry_runtime import EntryOrderRuntime

        return self._get_model("ti_entry_orders_v3", "entry_id", entry_id, EntryOrderRuntime)

    def entry_for_plan(self, plan_id: str):
        from .entry_runtime import EntryOrderRuntime

        return self._get_model("ti_entry_orders_v3", "plan_id", plan_id, EntryOrderRuntime)

    def list_entry_orders(
        self,
        *,
        symbol: str | None = None,
        active_only: bool = False,
        needs_reconciliation: bool = False,
        mode: str | None = None,
        limit: int | None = None,
    ):
        from .entry_runtime import ACTIVE_ENTRY_STATUSES, EntryOrderRuntime

        query = "SELECT payload FROM ti_entry_orders_v3"
        params: list[Any] = []
        clauses = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if active_only:
            clauses.append("status IN (" + ",".join("?" for _ in ACTIVE_ENTRY_STATUSES) + ")")
            params.extend(sorted(ACTIVE_ENTRY_STATUSES))
        if needs_reconciliation:
            # Keep this predicate identical to the partial reconciliation index.
            clauses.append(
                "(status IN ('SUBMITTING', 'OPEN', 'PARTIALLY_FILLED', "
                "'CANCEL_PENDING', 'UNCERTAIN') OR "
                "COALESCE(json_extract(payload, '$.terminal_notified'), 0) = 0)"
            )
        if mode is not None:
            clauses.append("mode = ?")
            params.append(mode)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(max(1, min(limit, 1000)))
        return self._list_models(query, params, EntryOrderRuntime)

    def create_entry_order(self, entry):
        """Atomically reserve a direction before any exchange side effect."""
        from .entry_runtime import ACTIVE_ENTRY_STATUSES, EntryOrderRuntime

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM ti_entry_orders_v3 WHERE plan_id = ?",
                (entry.plan_id,),
            ).fetchone()
            if existing:
                return EntryOrderRuntime.model_validate_json(existing["payload"]), False
            placeholders = ",".join("?" for _ in ACTIVE_ENTRY_STATUSES)
            pending = connection.execute(
                "SELECT entry_id FROM ti_entry_orders_v3 WHERE mode = ? AND symbol = ? "
                f"AND side = ? AND status IN ({placeholders}) LIMIT 1",
                (entry.mode, entry.symbol, entry.side, *sorted(ACTIVE_ENTRY_STATUSES)),
            ).fetchone()
            if pending:
                raise ValueError("this direction already has an unfinished entry order")
            self._write_entry(connection, entry, insert=True)
        return entry, True

    @staticmethod
    def _write_entry(connection, entry, *, insert: bool = False):
        if insert:
            connection.execute(
                "INSERT INTO ti_entry_orders_v3 "
                "(entry_id,plan_id,symbol,side,mode,status,version,payload,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry.entry_id,
                    entry.plan_id,
                    entry.symbol,
                    entry.side,
                    entry.mode,
                    entry.status,
                    entry.version,
                    entry.model_dump_json(),
                    entry.updated_at.isoformat(),
                ),
            )
        else:
            connection.execute(
                "UPDATE ti_entry_orders_v3 SET status=?,version=?,payload=?,updated_at=? "
                "WHERE entry_id=?",
                (
                    entry.status,
                    entry.version,
                    entry.model_dump_json(),
                    entry.updated_at.isoformat(),
                    entry.entry_id,
                ),
            )

    def update_entry_order(self, entry_id: str, *, expected_version: int, updates: dict):
        from .entry_runtime import EntryOrderRuntime

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,version FROM ti_entry_orders_v3 WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None or row["version"] != expected_version:
                raise RuntimeError("concurrent entry order update")
            entry = EntryOrderRuntime.model_validate_json(row["payload"])
            updated = EntryOrderRuntime.model_validate(
                {
                    **entry.model_dump(),
                    **updates,
                    "version": expected_version + 1,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._write_entry(connection, updated)
        return updated

    def merge_entry_fills(self, entry_id: str):
        """Book cumulative venue fills exactly once together with the direction pool."""
        from .entry_runtime import EntryOrderRuntime

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM ti_entry_orders_v3 WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise KeyError(entry_id)
            entry = EntryOrderRuntime.model_validate_json(row["payload"])
            plan_row = connection.execute(
                "SELECT payload FROM ti_plans_v2 WHERE plan_id=?",
                (entry.plan_id,),
            ).fetchone()
            plan = TradePlan.model_validate_json(plan_row["payload"])
            candidates = connection.execute(
                "SELECT payload FROM ti_positions_v2 WHERE symbol=? "
                "AND state NOT IN ('CLOSED','FLAT')",
                (entry.symbol,),
            ).fetchall()
            positions = [PositionRuntimeV2.model_validate_json(r["payload"]) for r in candidates]
            position = next((p for p in positions if p.side == entry.side), None)
            delta = entry.filled_quantity - entry.accounted_quantity
            if delta < 0:
                raise RuntimeError("venue cumulative fill quantity regressed")
            if not delta:
                if position is None or position.position_id != entry.position_id:
                    raise RuntimeError("accounted entry has no active attributable position")
                return entry, position
            if entry.average_fill_price is None:
                raise ValueError("actual fill price is required before booking a position")
            notional = entry.filled_quantity * entry.average_fill_price
            delta_notional = notional - entry.accounted_notional
            if delta_notional <= 0:
                raise RuntimeError("venue cumulative fill notional regressed")
            now = datetime.now(UTC)
            if position is None:
                if plan.position_id:
                    raise RuntimeError("add order filled after its target position was closed")
                position = PositionRuntimeV2(
                    plan_id=plan.plan_id,
                    episode_id=entry.episode_id,
                    plan_ids=[plan.plan_id],
                    episode_ids=[entry.episode_id],
                    symbol=entry.symbol,
                    side=entry.side,
                    state=PositionStateV2.OPEN,
                    original_quantity=delta,
                    remaining_quantity=delta,
                    average_entry=delta_notional / delta,
                    initial_stop=plan.management.hard_stop,
                    current_stop=plan.management.hard_stop,
                    entry_order_ids=entry.order_ids,
                    management_quantity=delta,
                    management_started_at=now,
                    entry_filled_quantities={entry_id: entry.filled_quantity},
                )
                connection.execute(
                    "INSERT INTO ti_positions_v2 "
                    "(position_id,plan_id,version,symbol,state,payload,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        position.position_id,
                        position.plan_id,
                        position.version,
                        position.symbol,
                        str(position.state),
                        position.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                if plan.position_id != position.position_id and position.plan_id != plan.plan_id:
                    raise RuntimeError("entry filled into an unexpected direction position")
                first = entry.accounted_quantity == 0
                remaining = position.remaining_quantity + delta
                updates = {
                    "plan_id": plan.plan_id if first else position.plan_id,
                    "episode_id": entry.episode_id if first else position.episode_id,
                    "plan_ids": list(
                        dict.fromkeys([position.plan_id, *position.plan_ids, plan.plan_id])
                    ),
                    "episode_ids": list(
                        dict.fromkeys(
                            [
                                *([position.episode_id] if position.episode_id else []),
                                *position.episode_ids,
                                entry.episode_id,
                            ]
                        )
                    ),
                    "entry_order_ids": list(
                        dict.fromkeys([*position.entry_order_ids, *entry.order_ids])
                    ),
                    "entry_filled_quantities": {
                        **position.entry_filled_quantities,
                        entry_id: entry.filled_quantity,
                    },
                    "original_quantity": position.original_quantity + delta,
                    "remaining_quantity": remaining,
                    "average_entry": (
                        position.average_entry * position.remaining_quantity + delta_notional
                    )
                    / remaining,
                    "management_quantity": remaining
                    if first
                    else (position.management_quantity or position.remaining_quantity) + delta,
                    "management_started_at": now if first else position.management_started_at,
                    "management_version": position.management_version + (1 if first else 0),
                    "current_stop": plan.management.hard_stop if first else position.current_stop,
                    "targets_reached": 0 if first else position.targets_reached,
                    "state": PositionStateV2.OPEN,
                    "version": position.version + 1,
                    "updated_at": now,
                }
                position = PositionRuntimeV2.model_validate({**position.model_dump(), **updates})
                connection.execute(
                    "UPDATE ti_positions_v2 SET plan_id=?,version=?,state=?,payload=?,updated_at=? "
                    "WHERE position_id=?",
                    (
                        position.plan_id,
                        position.version,
                        str(position.state),
                        position.model_dump_json(),
                        now.isoformat(),
                        position.position_id,
                    ),
                )
            entry = EntryOrderRuntime.model_validate(
                {
                    **entry.model_dump(),
                    "accounted_quantity": entry.filled_quantity,
                    "accounted_notional": notional,
                    "reserved_stop_risk": Decimal("0"),
                    "position_id": position.position_id,
                    "version": entry.version + 1,
                    "updated_at": now,
                }
            )
            self._write_entry(connection, entry)
        return entry, position

    def list_positions(
        self, *, symbol: str | None = None, include_closed: bool = False
    ) -> list[PositionRuntimeV2]:
        query = "SELECT payload FROM ti_positions_v2"
        clauses: list[str] = []
        params: list[Any] = []
        if not include_closed:
            clauses.append("state NOT IN ('CLOSED', 'FLAT')")
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC"
        return self._list_models(query, params, PositionRuntimeV2)

    def save_quant_prediction(self, prediction: QuantPrediction) -> None:
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO ti_quant_predictions_v1
                    (prediction_id, symbol, model_fingerprint, data_as_of, expires_at,
                     payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.prediction_id,
                    prediction.symbol,
                    prediction.model_fingerprint,
                    prediction.data_as_of.isoformat(),
                    prediction.expires_at.isoformat(),
                    prediction.model_dump_json(exclude_none=True),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def latest_quant_prediction(self, symbol: str) -> QuantPrediction | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM ti_quant_predictions_v1
                WHERE symbol = ? ORDER BY data_as_of DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
        return QuantPrediction.model_validate_json(row["payload"]) if row else None

    def save_episode(self, episode: DecisionEpisode) -> None:
        episode.updated_at = datetime.now(UTC)
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO ti_decision_episodes_v2
                    (episode_id, symbol, status, market_state_hash, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    episode.episode_id,
                    episode.symbol,
                    episode.status,
                    episode.market_state_hash,
                    episode.model_dump_json(exclude_none=True),
                    episode.created_at.isoformat(),
                    episode.updated_at.isoformat(),
                ),
            )

    def save_artifact(self, artifact_id: str, artifact_type: str, artifact: BaseModel) -> None:
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO ti_artifacts_v2
                    (artifact_id, artifact_type, content_hash, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    artifact_type,
                    content_hash(artifact),
                    artifact.model_dump_json(
                        exclude_none=True,
                        exclude_computed_fields=True,
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_artifacts(self, artifact_type: str, *, limit: int = 1000) -> list[dict[str, Any]]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, content_hash, payload, created_at
                FROM ti_artifacts_v2 WHERE artifact_type = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (artifact_type, limit),
            ).fetchall()
        return [
            {
                "artifact_id": str(row["artifact_id"]),
                "artifact_type": artifact_type,
                "content_hash": str(row["content_hash"]),
                "payload": json.loads(row["payload"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def save_wait_runtime(self, runtime: WaitRuntimeV2) -> None:
        self.save_artifact(runtime.wait_runtime_id, "WaitRuntimeV2", runtime)

    def list_wait_runtimes(
        self,
        *,
        symbol: str | None = None,
        active_only: bool = False,
    ) -> list[WaitRuntimeV2]:
        # Filter before parsing and before applying a history limit. Otherwise
        # every ticker decodes 1000 terminal waits, and an older active wait can
        # disappear from automation after 1000 newer artifacts are saved.
        clauses = ["artifact_type = 'WaitRuntimeV2'"]
        parameters: list[Any] = []
        if symbol:
            clauses.append("json_extract(payload, '$.symbol') = ?")
            parameters.append(symbol)
        if active_only:
            clauses.append("json_extract(payload, '$.status') = 'ACTIVE'")
        query = "SELECT payload FROM ti_artifacts_v2 WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        if not active_only:
            query += " LIMIT 1000"
        with closing(self.store.connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        runtimes = [WaitRuntimeV2.model_validate_json(row["payload"]) for row in rows]
        return sorted(runtimes, key=lambda item: item.updated_at, reverse=True)

    def update_wait_runtime(
        self,
        wait_runtime_id: str,
        *,
        expected_version: int,
        status: WaitRuntimeStatus,
    ) -> WaitRuntimeV2:
        artifact = self.get_artifact(wait_runtime_id)
        if not artifact:
            raise KeyError(wait_runtime_id)
        runtime = WaitRuntimeV2.model_validate(artifact["payload"])
        if runtime.version != expected_version:
            raise RuntimeError("concurrent wait runtime update")
        updated = runtime.model_copy(
            update={
                "version": expected_version + 1,
                "status": status,
                "updated_at": datetime.now(UTC),
            }
        )
        self.save_wait_runtime(updated)
        return updated

    def cancel_active_waits(self, symbol: str) -> list[WaitRuntimeV2]:
        cancelled = []
        for runtime in self.list_wait_runtimes(symbol=symbol, active_only=True):
            cancelled.append(
                self.update_wait_runtime(
                    runtime.wait_runtime_id,
                    expected_version=runtime.version,
                    status=WaitRuntimeStatus.CANCELLED,
                )
            )
        return cancelled

    def automation_runtime(self) -> AutomationRuntimeV2:
        artifact = self.get_artifact("trading-intelligence-v2")
        return (
            AutomationRuntimeV2.model_validate(artifact["payload"])
            if artifact
            else AutomationRuntimeV2()
        )

    def save_automation_runtime(
        self,
        runtime: AutomationRuntimeV2,
        *,
        expected_updated_at: datetime | None = None,
    ) -> bool:
        # Serialize the comparison and write with configuration changes in worker threads.
        with self.store._lock:
            artifact = self.get_artifact(runtime.runtime_id)
            if (
                expected_updated_at is not None
                and artifact is not None
                and AutomationRuntimeV2.model_validate(artifact["payload"]).updated_at
                != expected_updated_at
            ):
                return False
            if not runtime.enabled and runtime.status != AutomationRuntimeStatus.HALTED:
                runtime = runtime.model_copy(update={"status": AutomationRuntimeStatus.STOPPED})
            self.save_artifact(runtime.runtime_id, "AutomationRuntimeV2", runtime)
            return True

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                """
                SELECT artifact_type, content_hash, payload, created_at
                FROM ti_artifacts_v2 WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        return (
            {
                "artifact_id": artifact_id,
                "artifact_type": row["artifact_type"],
                "content_hash": row["content_hash"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            if row
            else None
        )

    def list_episodes(
        self, *, symbol: str | None = None, limit: int = 100
    ) -> list[DecisionEpisode]:
        query = "SELECT payload FROM ti_decision_episodes_v2"
        params: list[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return self._list_models(query, params, DecisionEpisode)

    def append_event(
        self,
        *,
        dedupe_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> tuple[str, bool]:
        event_id = f"tie_{uuid4().hex}"
        with self.store._lock, self.store.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO ti_events_v2
                        (event_id, dedupe_key, event_type, aggregate_type,
                         aggregate_id, aggregate_version, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        dedupe_key,
                        event_type,
                        aggregate_type,
                        aggregate_id,
                        aggregate_version,
                        _json(payload),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                return event_id, True
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT event_id FROM ti_events_v2 WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if not row:
                    raise
                return str(row["event_id"]), False

    def fire_wake_once(
        self,
        *,
        condition_id: str,
        event_key: str,
        aggregate_id: str,
        aggregate_version: int,
        payload: dict[str, Any],
    ) -> tuple[str, bool]:
        """Atomically persist one wake firing and its ledger event."""

        event_id = f"tie_{uuid4().hex}"
        # LLM-authored condition ids are labels, not globally unique identities.
        # Scope them to their owning plan/wait runtime so a later plan may safely
        # reuse the same human-readable id while each condition instance still
        # fires exactly once across restarts.
        condition_instance_id = f"{aggregate_id}:{condition_id}"
        dedupe_key = f"wake:{condition_instance_id}:{event_key}"
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT event_id FROM ti_wake_firings_v2
                WHERE condition_id = ?
                """,
                (condition_instance_id,),
            ).fetchone()
            if existing:
                return str(existing["event_id"]), False
            connection.execute(
                """
                INSERT INTO ti_events_v2
                    (event_id, dedupe_key, event_type, aggregate_type,
                     aggregate_id, aggregate_version, payload, created_at)
                VALUES (?, ?, 'WAKE_TRIGGERED', 'plan', ?, ?, ?, ?)
                """,
                (
                    event_id,
                    dedupe_key,
                    aggregate_id,
                    aggregate_version,
                    _json(payload),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO ti_wake_firings_v2
                    (condition_id, event_key, event_id, fired_at)
                VALUES (?, ?, ?, ?)
                """,
                (condition_instance_id, event_key, event_id, now),
            )
        return event_id, True

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT event_id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, payload, created_at
                FROM ti_events_v2 WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "aggregate_type": str(row["aggregate_type"]),
            "aggregate_id": str(row["aggregate_id"]),
            "aggregate_version": int(row["aggregate_version"]),
            "payload": json.loads(row["payload"]),
            "created_at": str(row["created_at"]),
        }

    def issue_grant(self, grant: HumanLiveGrant) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE ti_live_grants_v2
                SET status = 'SUPERSEDED', updated_at = ?
                WHERE status = 'ACTIVE'
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO ti_live_grants_v2
                    (grant_id, status, configuration_fingerprint, expires_at,
                     payload, created_at, updated_at)
                VALUES (?, 'ACTIVE', ?, ?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    grant.configuration_fingerprint,
                    grant.expires_at.isoformat(),
                    grant.model_dump_json(exclude_none=True),
                    grant.issued_at.isoformat(),
                    now,
                ),
            )

    def active_grant(self, *, now: datetime | None = None) -> HumanLiveGrant | None:
        current = now or datetime.now(UTC)
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM ti_live_grants_v2
                WHERE status = 'ACTIVE' AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (current.isoformat(),),
            ).fetchone()
        if not row:
            return None
        grant = HumanLiveGrant.model_validate_json(row["payload"])
        return grant if grant.revoked_at is None else None

    def revoke_grant(self, grant_id: str) -> HumanLiveGrant:
        now = datetime.now(UTC)
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM ti_live_grants_v2 WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if not row:
                raise KeyError(grant_id)
            grant = HumanLiveGrant.model_validate_json(row["payload"])
            revoked = grant.model_copy(update={"revoked_at": now})
            connection.execute(
                """
                UPDATE ti_live_grants_v2
                SET status = 'REVOKED', payload = ?, updated_at = ?
                WHERE grant_id = ?
                """,
                (revoked.model_dump_json(exclude_none=True), now.isoformat(), grant_id),
            )
        return revoked

    def next_queued_decision(self, symbol: str) -> dict[str, Any] | None:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT sequence FROM ti_decision_queue_cursors_v2 WHERE symbol = ?",
                (symbol,),
            ).fetchone()
            sequence = cursor["sequence"] if cursor else 0
            row = connection.execute(
                "SELECT queued.* FROM ti_events_v2 queued "
                "WHERE queued.event_type = 'AUTOMATION_DECISION_QUEUED' "
                "AND queued.aggregate_id = ? AND queued.sequence > ? "
                "AND NOT EXISTS (SELECT 1 FROM ti_events_v2 done "
                "WHERE done.dedupe_key = 'automation-dequeued:' || queued.event_id) "
                "ORDER BY queued.sequence LIMIT 1",
                (symbol, sequence),
            ).fetchone()
            if row is None:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM ti_events_v2 "
                    "WHERE event_type = 'AUTOMATION_DECISION_QUEUED' AND aggregate_id = ?",
                    (symbol,),
                ).fetchone()[0]
                if cursor is None or latest > sequence:
                    connection.execute(
                        "INSERT INTO ti_decision_queue_cursors_v2 VALUES (?, ?) "
                        "ON CONFLICT(symbol) DO UPDATE SET "
                        "sequence = MAX(sequence, excluded.sequence)",
                        (symbol, latest),
                    )
                return None
        return {**dict(row), "payload": json.loads(row["payload"])}

    def advance_decision_cursor(self, symbol: str, sequence: int) -> None:
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                "INSERT INTO ti_decision_queue_cursors_v2 VALUES (?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET sequence = MAX(sequence, excluded.sequence)",
                (symbol, sequence),
            )

    def list_events(
        self, *, aggregate_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ti_events_v2"
        params: list[Any] = []
        if aggregate_id:
            query += " WHERE aggregate_id = ?"
            params.append(aggregate_id)
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def _get_model(
        self, table: str, key_column: str, key: str, model: type[ModelT]
    ) -> ModelT | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
        return model.model_validate_json(row["payload"]) if row else None

    def _list_models(self, query: str, params: list[Any], model: type[ModelT]) -> list[ModelT]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [model.model_validate_json(row["payload"]) for row in rows]
