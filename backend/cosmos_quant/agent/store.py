from __future__ import annotations

from datetime import UTC, datetime

from ..storage import SqliteStore
from .schemas import TradeEpisode
from .shadow_validation import ShadowOutcomeStatus, ShadowValidationTrial


class AgentStore:
    """Read-only legacy episode evidence plus offline Shadow research storage.

    The former goal, event, scheduler, run, tool-call, and execution mutation
    methods intentionally do not exist. Historical trade episodes remain
    queryable so certificates and audit reports can still resolve their lineage.
    """

    def __init__(self, store: SqliteStore):
        self.store = store

    def list_episodes(self, limit: int = 50) -> list[TradeEpisode]:
        bounded = min(max(limit, 1), 10_000)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM trade_episodes ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [TradeEpisode.model_validate_json(row["payload"]) for row in rows]

    def save_shadow_trial(self, trial: ShadowValidationTrial) -> None:
        """Persist an offline counterfactual research trial."""

        trial.updated_at = datetime.now(UTC)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_shadow_trials
                    (trial_id, plan_id, goal_id, run_id, symbol, timeframe,
                     status, payload, decision_at, horizon_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trial_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    trial.id,
                    trial.plan.id,
                    trial.goal_id,
                    trial.run_id,
                    trial.plan.symbol,
                    trial.plan.timeframe,
                    str(trial.outcome.status),
                    trial.model_dump_json(),
                    trial.decision_at.isoformat(),
                    trial.horizon_at.isoformat(),
                    trial.updated_at.isoformat(),
                ),
            )

    def list_shadow_trials(
        self,
        limit: int = 500,
        *,
        goal_id: str | None = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        terminal: bool | None = None,
    ) -> list[ShadowValidationTrial]:
        bounded = min(max(limit, 1), 10_000)
        terminal_statuses = {
            str(ShadowOutcomeStatus.NO_ENTRY),
            str(ShadowOutcomeStatus.CLOSED),
            str(ShadowOutcomeStatus.HORIZON_MARK),
        }
        clauses = ["(? IS NULL OR goal_id = ?)", "(? IS NULL OR symbol = ?)"]
        clauses.append("(? IS NULL OR timeframe = ?)")
        arguments: list = [goal_id, goal_id, symbol, symbol, timeframe, timeframe]
        if terminal is not None:
            placeholders = ", ".join("?" for _ in terminal_statuses)
            clauses.append(f"status {'IN' if terminal else 'NOT IN'} ({placeholders})")
            arguments.extend(sorted(terminal_statuses))
        arguments.append(bounded)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM agent_shadow_trials
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                arguments,
            ).fetchall()
        return [ShadowValidationTrial.model_validate_json(row["payload"]) for row in rows]
