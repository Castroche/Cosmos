from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from ..storage import SqliteStore
from .policy import AgentPolicy
from .schemas import (
    AgentAutonomyMode,
    AgentDecision,
    AgentEvent,
    AgentEventStatus,
    AgentEventType,
    AgentGoal,
    AgentGoalStatus,
    AgentMemory,
    AgentObservation,
    AgentRun,
    AgentRunStatus,
    AgentToolCall,
    TradeEpisode,
    TradeEpisodeStatus,
)
from .shadow_validation import ShadowOutcomeStatus, ShadowValidationTrial


class AgentStore:
    """Durable Agent state built on the same transactional SQLite boundary."""

    def __init__(self, store: SqliteStore):
        self.store = store

    def save_goal(self, goal: AgentGoal) -> None:
        now = datetime.now(UTC)
        goal.updated_at = now
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_goals
                    (goal_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    goal.id,
                    str(goal.status),
                    goal.model_dump_json(),
                    goal.created_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def load_goal(self, goal_id: str) -> AgentGoal | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_goals WHERE goal_id = ?", (goal_id,)
            ).fetchone()
        return AgentGoal.model_validate_json(row["payload"]) if row else None

    def list_goals(self, limit: int = 50) -> list[AgentGoal]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_goals ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [AgentGoal.model_validate_json(row["payload"]) for row in rows]

    def save_policy(self, policy: AgentPolicy) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_policies
                    (policy_id, autonomy_mode, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    autonomy_mode = excluded.autonomy_mode,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.id,
                    str(policy.autonomy_mode),
                    policy.model_dump_json(),
                    now,
                    now,
                ),
            )

    def load_policy(self, policy_id: str) -> AgentPolicy | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_policies WHERE policy_id = ?", (policy_id,)
            ).fetchone()
        return AgentPolicy.model_validate_json(row["payload"]) if row else None

    def list_policies(self, limit: int = 50) -> list[AgentPolicy]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_policies ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [AgentPolicy.model_validate_json(row["payload"]) for row in rows]

    def save_launch_bundle(self, policy: AgentPolicy, goal: AgentGoal, profile) -> None:
        """Persist a quick-launch grant, goal, and remembered profile atomically."""

        now = datetime.now(UTC)
        goal.updated_at = now
        profile.updated_at = now
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO agent_policies
                    (policy_id, autonomy_mode, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    autonomy_mode = excluded.autonomy_mode,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.id,
                    str(policy.autonomy_mode),
                    policy.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_goals
                    (goal_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    goal.id,
                    str(goal.status),
                    goal.model_dump_json(),
                    goal.created_at.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_launch_profiles
                    (profile_id, autonomy_mode, grant_fingerprint,
                     authorized_fingerprint, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    autonomy_mode = excluded.autonomy_mode,
                    grant_fingerprint = excluded.grant_fingerprint,
                    authorized_fingerprint = excluded.authorized_fingerprint,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.id,
                    str(profile.autonomy_mode),
                    profile.grant_fingerprint,
                    profile.authorized_fingerprint,
                    profile.model_dump_json(),
                    profile.created_at.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()

    def promote_goal_to_live(
        self,
        source_goal_id: str,
        live_policy: AgentPolicy,
        live_goal: AgentGoal,
    ) -> tuple[AgentGoal, AgentPolicy]:
        """Atomically pause sandbox execution goals and activate one live successor."""

        if live_policy.autonomy_mode != AgentAutonomyMode.LIVE_CAPPED:
            raise ValueError("promoted policy must use live_capped autonomy")
        if (
            live_goal.autonomy_mode != AgentAutonomyMode.LIVE_CAPPED
            or live_goal.risk_policy_id != live_policy.id
        ):
            raise ValueError("promoted goal must bind the promoted live policy")
        now = datetime.now(UTC)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                "SELECT payload FROM agent_goals WHERE goal_id = ?",
                (source_goal_id,),
            ).fetchone()
            if not source_row:
                raise RuntimeError("sandbox Agent goal no longer exists")
            source = AgentGoal.model_validate_json(source_row["payload"])
            existing_row = connection.execute(
                "SELECT payload FROM agent_goals WHERE goal_id = ?",
                (live_goal.id,),
            ).fetchone()
            if source.status != AgentGoalStatus.ACTIVE:
                if existing_row:
                    existing = AgentGoal.model_validate_json(existing_row["payload"])
                    if (
                        existing.autonomy_mode == AgentAutonomyMode.LIVE_CAPPED
                        and existing.risk_policy_id == live_policy.id
                    ):
                        return existing, live_policy
                raise RuntimeError("sandbox Agent goal is no longer active")
            if source.autonomy_mode != AgentAutonomyMode.SANDBOX:
                raise RuntimeError("only an active sandbox Agent goal can be promoted")

            live_goal.status = AgentGoalStatus.ACTIVE
            live_goal.updated_at = now
            connection.execute(
                """
                INSERT INTO agent_policies
                    (policy_id, autonomy_mode, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    autonomy_mode = excluded.autonomy_mode,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    live_policy.id,
                    str(live_policy.autonomy_mode),
                    live_policy.model_dump_json(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            active_rows = connection.execute(
                "SELECT goal_id, payload FROM agent_goals WHERE status = ?",
                (str(AgentGoalStatus.ACTIVE),),
            ).fetchall()
            for row in active_rows:
                goal = AgentGoal.model_validate_json(row["payload"])
                if goal.autonomy_mode != AgentAutonomyMode.SANDBOX:
                    continue
                goal.status = AgentGoalStatus.PAUSED
                goal.updated_at = now
                connection.execute(
                    """
                    UPDATE agent_goals
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE goal_id = ?
                    """,
                    (
                        str(goal.status),
                        goal.model_dump_json(),
                        now.isoformat(),
                        goal.id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO agent_goals
                    (goal_id, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(goal_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    live_goal.id,
                    str(live_goal.status),
                    live_goal.model_dump_json(),
                    live_goal.created_at.isoformat(),
                    now.isoformat(),
                ),
            )
        return live_goal, live_policy

    def append_event(self, event: AgentEvent) -> AgentEvent:
        """Append once by semantic dedupe key and return the canonical event."""

        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM agent_events WHERE dedupe_key = ?",
                (event.dedupe_key,),
            ).fetchone()
            if existing:
                connection.commit()
                return AgentEvent.model_validate_json(existing["payload"])
            connection.execute(
                """
                INSERT INTO agent_events
                    (event_id, event_type, source, goal_id, dedupe_key, status, payload,
                     occurred_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    str(event.event_type),
                    event.source,
                    event.goal_id,
                    event.dedupe_key,
                    str(AgentEventStatus.PENDING),
                    event.model_dump_json(),
                    event.occurred_at.isoformat(),
                    event.created_at.isoformat(),
                ),
            )
            connection.commit()
        return event

    def supersede_stale_schedule_events(
        self,
        goal_id: str,
        active_dedupe_keys: list[str],
        *,
        stale_claim_seconds: int = 120,
    ) -> int:
        """Coalesce scheduler backlog while preserving every row for audit.

        A scheduled run always re-reads the latest closed candles, so replaying
        hundreds of older schedule ticks adds cost without restoring information.
        Operator, order, position and risk events are intentionally untouched.
        """

        if not active_dedupe_keys:
            return 0
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(stale_claim_seconds, 1))
        placeholders = ", ".join("?" for _ in active_dedupe_keys)
        with self.store.connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE agent_events
                SET status = ?, processed_at = ?
                WHERE goal_id = ?
                  AND source = 'agent-scheduler'
                  AND event_type = ?
                  AND dedupe_key NOT IN ({placeholders})
                  AND (
                      status = ?
                      OR (status = ? AND claimed_at < ?)
                  )
                """,
                (
                    str(AgentEventStatus.SUPERSEDED),
                    now.isoformat(),
                    goal_id,
                    str(AgentEventType.SCHEDULE_TRIGGERED),
                    *active_dedupe_keys,
                    str(AgentEventStatus.PENDING),
                    str(AgentEventStatus.CLAIMED),
                    cutoff.isoformat(),
                ),
            ).rowcount
        return updated

    def requeue_safe_failed_schedule_event(
        self,
        dedupe_key: str,
        *,
        max_attempts: int = 3,
        backoff_seconds: int = 30,
    ) -> bool:
        """Retry a scheduler event only when no Agent action was attempted.

        A transient exchange failure can occur while the initial state is being
        assembled. The event's candle-scoped dedupe key would otherwise suppress
        every retry until the next 1h/4h bucket. Reusing the durable event is safe
        only when the latest run failed at step zero: no decision, tool call,
        authorization, or order submission can have occurred.
        """

        if max_attempts <= 1:
            return False
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(backoff_seconds, 0))
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event_row = connection.execute(
                """
                SELECT event_id, status, processed_at
                FROM agent_events
                WHERE dedupe_key = ?
                  AND source = 'agent-scheduler'
                  AND event_type = ?
                """,
                (
                    dedupe_key,
                    str(AgentEventType.SCHEDULE_TRIGGERED),
                ),
            ).fetchone()
            if (
                not event_row
                or event_row["status"] != str(AgentEventStatus.FAILED)
                or not event_row["processed_at"]
                or datetime.fromisoformat(event_row["processed_at"]) > cutoff
            ):
                connection.commit()
                return False
            run_rows = connection.execute(
                """
                SELECT payload
                FROM agent_runs
                WHERE trigger_event_id = ?
                ORDER BY created_at DESC
                """,
                (event_row["event_id"],),
            ).fetchall()
            if not run_rows or len(run_rows) >= max_attempts:
                connection.commit()
                return False
            latest = AgentRun.model_validate_json(run_rows[0]["payload"])
            if latest.status != AgentRunStatus.FAILED or latest.step_count != 0:
                connection.commit()
                return False
            updated = connection.execute(
                """
                UPDATE agent_events
                SET status = ?, claimed_by = NULL, claimed_at = NULL,
                    processed_at = NULL
                WHERE event_id = ? AND status = ?
                """,
                (
                    str(AgentEventStatus.PENDING),
                    event_row["event_id"],
                    str(AgentEventStatus.FAILED),
                ),
            ).rowcount
            connection.commit()
        return bool(updated)

    def claim_next_event(
        self, worker_id: str, *, goal_id: str | None = None, lease_seconds: float = 60
    ) -> AgentEvent | None:
        cutoff = datetime.now(UTC) - timedelta(seconds=lease_seconds)
        claimed_at = datetime.now(UTC)
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT event_id, payload FROM agent_events
                WHERE (? IS NULL OR goal_id = ?) AND (
                    status = ? OR (status = ? AND claimed_at < ?)
                )
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (
                    goal_id,
                    goal_id,
                    str(AgentEventStatus.PENDING),
                    str(AgentEventStatus.CLAIMED),
                    cutoff.isoformat(),
                ),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            updated = connection.execute(
                """
                UPDATE agent_events
                SET status = ?, claimed_by = ?, claimed_at = ?
                WHERE event_id = ? AND (
                    status = ? OR (status = ? AND claimed_at < ?)
                )
                """,
                (
                    str(AgentEventStatus.CLAIMED),
                    worker_id,
                    claimed_at.isoformat(),
                    row["event_id"],
                    str(AgentEventStatus.PENDING),
                    str(AgentEventStatus.CLAIMED),
                    cutoff.isoformat(),
                ),
            ).rowcount
            connection.commit()
        return AgentEvent.model_validate_json(row["payload"]) if updated else None

    def finish_event(self, event_id: str, worker_id: str, *, failed: bool = False) -> None:
        status = AgentEventStatus.FAILED if failed else AgentEventStatus.PROCESSED
        with self.store.connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_events
                SET status = ?, processed_at = ?
                WHERE event_id = ? AND status = ? AND claimed_by = ?
                """,
                (
                    str(status),
                    datetime.now(UTC).isoformat(),
                    event_id,
                    str(AgentEventStatus.CLAIMED),
                    worker_id,
                ),
            ).rowcount
        if not updated:
            raise RuntimeError("agent event lease is not owned by this worker")

    def renew_event_lease(self, event_id: str, worker_id: str) -> None:
        with self.store.connect() as connection:
            updated = connection.execute(
                """
                UPDATE agent_events SET claimed_at = ?
                WHERE event_id = ? AND status = ? AND claimed_by = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    event_id,
                    str(AgentEventStatus.CLAIMED),
                    worker_id,
                ),
            ).rowcount
        if not updated:
            raise RuntimeError("agent event lease cannot be renewed by this worker")

    def save_run(self, run: AgentRun) -> None:
        now = datetime.now(UTC)
        run.updated_at = now
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs
                    (run_id, goal_id, trigger_event_id, status, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    run.id,
                    run.goal_id,
                    run.trigger_event_id,
                    str(run.status),
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def load_run(self, run_id: str) -> AgentRun | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return AgentRun.model_validate_json(row["payload"]) if row else None

    def list_runs(self, limit: int = 50) -> list[AgentRun]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_runs ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [AgentRun.model_validate_json(row["payload"]) for row in rows]

    def schedule_market_coverage(
        self,
        goal_ids: list[str],
        *,
        limit: int = 2000,
    ) -> list[dict]:
        """Return the latest durable scheduler/run status per market scope."""

        scoped_goal_ids = list(dict.fromkeys(goal_ids))
        if not scoped_goal_ids:
            return []
        bounded = min(max(limit, 1), 10000)
        placeholders = ", ".join("?" for _ in scoped_goal_ids)
        with self.store.connect() as connection:
            event_rows = connection.execute(
                f"""
                SELECT event_id, goal_id, status, payload, created_at,
                       processed_at
                FROM agent_events
                WHERE source = 'agent-scheduler'
                  AND event_type = ?
                  AND goal_id IN ({placeholders})
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (
                    str(AgentEventType.SCHEDULE_TRIGGERED),
                    *scoped_goal_ids,
                    bounded,
                ),
            ).fetchall()
            latest_events: dict[tuple[str, str, str], dict] = {}
            for row in event_rows:
                event = AgentEvent.model_validate_json(row["payload"])
                symbol = str(event.payload.get("symbol") or "")
                timeframe = str(event.payload.get("timeframe") or "")
                if not symbol or not timeframe:
                    continue
                key = (event.goal_id or row["goal_id"], symbol, timeframe)
                if key in latest_events:
                    continue
                latest_events[key] = {
                    "event_id": row["event_id"],
                    "goal_id": key[0],
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "event_status": row["status"],
                    "event_created_at": row["created_at"],
                    "event_processed_at": row["processed_at"],
                    "dedupe_key": event.dedupe_key,
                }
            event_ids = [item["event_id"] for item in latest_events.values()]
            run_rows = []
            if event_ids:
                run_placeholders = ", ".join("?" for _ in event_ids)
                run_rows = connection.execute(
                    f"""
                    SELECT trigger_event_id, payload, created_at
                    FROM agent_runs
                    WHERE trigger_event_id IN ({run_placeholders})
                    ORDER BY created_at DESC
                    """,
                    tuple(event_ids),
                ).fetchall()

        attempts: dict[str, int] = {}
        latest_runs: dict[str, AgentRun] = {}
        for row in run_rows:
            event_id = row["trigger_event_id"]
            attempts[event_id] = attempts.get(event_id, 0) + 1
            if event_id not in latest_runs:
                latest_runs[event_id] = AgentRun.model_validate_json(row["payload"])
        coverage = []
        for item in latest_events.values():
            run = latest_runs.get(item["event_id"])
            coverage.append(
                {
                    **item,
                    "attempt_count": attempts.get(item["event_id"], 0),
                    "last_run_id": run.id if run else None,
                    "last_run_status": str(run.status) if run else None,
                    "last_run_steps": run.step_count if run else 0,
                    "last_run_error": run.last_error if run else "",
                    "retry_recovered": bool(
                        attempts.get(item["event_id"], 0) > 1
                        and item["event_status"] == str(AgentEventStatus.PROCESSED)
                    ),
                }
            )
        return coverage

    def load_open_run_for_event(self, event_id: str) -> AgentRun | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM agent_runs
                WHERE trigger_event_id = ? AND status = 'running'
                ORDER BY created_at DESC LIMIT 1
                """,
                (event_id,),
            ).fetchone()
        return AgentRun.model_validate_json(row["payload"]) if row else None

    def observations_for_run(self, run_id: str, limit: int = 50) -> list[AgentObservation]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM agent_observations
                WHERE run_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (run_id, bounded),
            ).fetchall()
        return [AgentObservation.model_validate_json(row["payload"]) for row in rows]

    def decisions_for_run(self, run_id: str, limit: int = 50) -> list[AgentDecision]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM agent_decisions
                WHERE run_id = ? ORDER BY created_at ASC LIMIT ?
                """,
                (run_id, bounded),
            ).fetchall()
        return [AgentDecision.model_validate_json(row["payload"]) for row in rows]

    def save_decision(self, decision: AgentDecision) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_decisions
                    (decision_id, run_id, action, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.run_id,
                    str(decision.action),
                    decision.model_dump_json(),
                    decision.created_at.isoformat(),
                ),
            )

    def save_observation(self, observation: AgentObservation) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_observations
                    (observation_id, run_id, tool_call_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    observation.id,
                    observation.run_id,
                    observation.tool_call_id,
                    observation.model_dump_json(),
                    observation.created_at.isoformat(),
                ),
            )

    def begin_tool_call(self, run_id: str, call: AgentToolCall) -> tuple[str, dict | None, bool]:
        """Claim a tool idempotency key, returning an existing terminal result if present."""

        now = datetime.now(UTC).isoformat()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT status, result_payload FROM agent_tool_calls
                WHERE idempotency_key = ?
                """,
                (call.idempotency_key,),
            ).fetchone()
            if existing:
                connection.commit()
                result = (
                    json.loads(existing["result_payload"]) if existing["result_payload"] else None
                )
                return str(existing["status"]), result, False
            connection.execute(
                """
                INSERT INTO agent_tool_calls
                    (tool_call_id, run_id, tool_name, idempotency_key, status,
                     request_payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    call.id,
                    run_id,
                    call.name,
                    call.idempotency_key,
                    call.model_dump_json(),
                    now,
                    now,
                ),
            )
            connection.commit()
        return "running", None, True

    def finish_tool_call(
        self,
        call: AgentToolCall,
        result: dict,
        *,
        failed: bool = False,
        observation: AgentObservation | None = None,
    ) -> None:
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE agent_tool_calls
                SET status = ?, result_payload = ?, updated_at = ?
                WHERE tool_call_id = ? AND status = 'running'
                """,
                (
                    "failed" if failed else "completed",
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    call.id,
                ),
            ).rowcount
            if not updated:
                raise RuntimeError("agent tool call is not in running state")
            if observation is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_observations
                        (observation_id, run_id, tool_call_id, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        observation.id,
                        observation.run_id,
                        observation.tool_call_id,
                        observation.model_dump_json(),
                        observation.created_at.isoformat(),
                    ),
                )
            connection.commit()

    def recover_interrupted_work(self) -> list[dict]:
        """Close work left RUNNING by a terminated process.

        Read-only and research calls can safely fail terminally because their
        idempotency key will return the persisted interruption observation.
        Calls that could have changed execution state are additionally reported
        as uncertain so the application can halt entries and reconcile first.
        """

        external_effect_tools = {
            "execute_validated_trade_plan",
            "cancel_entry_order",
            "reduce_position",
            "exit_position",
        }
        now = datetime.now(UTC)
        recovered: list[dict] = []
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT tool_call_id, run_id, tool_name, request_payload
                FROM agent_tool_calls
                WHERE status = 'running'
                ORDER BY created_at ASC
                """
            ).fetchall()
            for row in rows:
                call = AgentToolCall.model_validate_json(row["request_payload"])
                uncertain = row["tool_name"] in external_effect_tools
                observation = AgentObservation(
                    run_id=row["run_id"],
                    kind="tool_interrupted",
                    ok=False,
                    summary=(
                        f"Agent tool {row['tool_name']} was interrupted by process "
                        "termination; the call was not retried automatically"
                    ),
                    data={
                        "tool": row["tool_name"],
                        "recovery": "application_restart",
                        "external_effect_uncertain": uncertain,
                        "idempotency_key": call.idempotency_key,
                    },
                    tool_call_id=call.id,
                    created_at=now,
                )
                connection.execute(
                    """
                    UPDATE agent_tool_calls
                    SET status = 'failed', result_payload = ?, updated_at = ?
                    WHERE tool_call_id = ? AND status = 'running'
                    """,
                    (
                        json.dumps(
                            observation.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now.isoformat(),
                        row["tool_call_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_observations
                        (observation_id, run_id, tool_call_id, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        observation.id,
                        observation.run_id,
                        observation.tool_call_id,
                        observation.model_dump_json(),
                        observation.created_at.isoformat(),
                    ),
                )
                if uncertain:
                    plan_id = str(call.arguments.get("plan_id") or "")
                    if plan_id:
                        episode_row = connection.execute(
                            """
                            SELECT episode_id, payload
                            FROM trade_episodes
                            WHERE plan_id = ? AND status IN (?, ?, ?, ?)
                            ORDER BY updated_at DESC LIMIT 1
                            """,
                            (
                                plan_id,
                                str(TradeEpisodeStatus.RESERVED),
                                str(TradeEpisodeStatus.PENDING_ENTRY),
                                str(TradeEpisodeStatus.OPEN),
                                str(TradeEpisodeStatus.AWAITING_REVIEW),
                            ),
                        ).fetchone()
                        if episode_row:
                            episode = TradeEpisode.model_validate_json(episode_row["payload"])
                            episode.status = TradeEpisodeStatus.UNCERTAIN
                            episode.summary = (
                                f"Agent tool {row['tool_name']} was interrupted; "
                                "exchange reconciliation is required before new entries"
                            )
                            episode.updated_at = now
                            connection.execute(
                                """
                                UPDATE trade_episodes
                                SET status = ?, payload = ?, updated_at = ?
                                WHERE episode_id = ?
                                """,
                                (
                                    str(episode.status),
                                    episode.model_dump_json(),
                                    now.isoformat(),
                                    episode.id,
                                ),
                            )
                recovered.append(
                    {
                        "tool_call_id": row["tool_call_id"],
                        "run_id": row["run_id"],
                        "tool_name": row["tool_name"],
                        "external_effect_uncertain": uncertain,
                    }
                )

            run_rows = connection.execute(
                "SELECT run_id, payload FROM agent_runs WHERE status = 'running'"
            ).fetchall()
            for row in run_rows:
                run = AgentRun.model_validate_json(row["payload"])
                run.status = AgentRunStatus.FAILED
                run.last_error = (
                    "Agent run was interrupted by process termination and recovered "
                    "fail-closed on application restart"
                )
                run.updated_at = now
                run.completed_at = now
                connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (
                        str(run.status),
                        run.model_dump_json(),
                        now.isoformat(),
                        run.id,
                    ),
                )
            connection.commit()
        return recovered

    def save_memory(self, memory: AgentMemory) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_memories
                    (memory_id, memory_type, source_episode_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    memory.id,
                    str(memory.memory_type),
                    memory.source_episode_id,
                    memory.model_dump_json(),
                    memory.created_at.isoformat(),
                ),
            )

    def recent_memories(self, limit: int = 20) -> list[AgentMemory]:
        bounded = min(max(limit, 1), 200)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM agent_memories ORDER BY created_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [AgentMemory.model_validate_json(row["payload"]) for row in rows]

    def save_episode(self, episode: TradeEpisode) -> None:
        episode.updated_at = datetime.now(UTC)
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO trade_episodes
                    (episode_id, plan_id, symbol, status, payload, opened_at,
                     closed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    closed_at = excluded.closed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    episode.id,
                    episode.plan_id,
                    episode.symbol,
                    str(episode.status),
                    episode.model_dump_json(),
                    episode.opened_at.isoformat(),
                    episode.closed_at.isoformat() if episode.closed_at else None,
                    episode.updated_at.isoformat(),
                ),
            )

    def reserve_episode(
        self,
        episode: TradeEpisode,
        *,
        max_trades_per_day: int,
        max_open_positions: int,
    ) -> TradeEpisode:
        """Atomically reserve an Agent entry budget before touching an exchange."""

        if episode.status != "reserved":
            raise ValueError("entry budget can only be reserved for a reserved episode")
        if not episode.policy_id:
            raise ValueError("entry budget reservation requires a policy id")
        today = datetime.now(UTC).date()
        open_statuses = {"reserved", "pending_entry", "open", "uncertain"}
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT payload FROM trade_episodes").fetchall()
            episodes = [TradeEpisode.model_validate_json(row["payload"]) for row in rows]
            existing = next((item for item in episodes if item.plan_id == episode.plan_id), None)
            if existing:
                connection.rollback()
                raise RuntimeError("Agent trade plan already has an entry episode")
            scoped = [item for item in episodes if item.policy_id == episode.policy_id]
            trades_today = sum(
                item.opened_at.date() == today and str(item.status) != "canceled" for item in scoped
            )
            if max_trades_per_day <= 0 or trades_today >= max_trades_per_day:
                connection.rollback()
                raise RuntimeError("Agent daily trade budget is exhausted")
            same_symbol_episode = next(
                (
                    item
                    for item in scoped
                    if item.symbol == episode.symbol and str(item.status) in open_statuses
                ),
                None,
            )
            if same_symbol_episode:
                connection.rollback()
                raise RuntimeError(
                    f"Agent symbol already has an active entry episode {same_symbol_episode.id}"
                )
            open_episodes = sum(str(item.status) in open_statuses for item in scoped)
            if max_open_positions <= 0 or open_episodes >= max_open_positions:
                connection.rollback()
                raise RuntimeError("Agent open-position budget is exhausted")
            connection.execute(
                """
                INSERT INTO trade_episodes
                    (episode_id, plan_id, symbol, status, payload, opened_at,
                     closed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    episode.id,
                    episode.plan_id,
                    episode.symbol,
                    str(episode.status),
                    episode.model_dump_json(),
                    episode.opened_at.isoformat(),
                    episode.updated_at.isoformat(),
                ),
            )
            connection.commit()
        return episode

    def list_episodes(self, limit: int = 50) -> list[TradeEpisode]:
        bounded = min(max(limit, 1), 10000)
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM trade_episodes ORDER BY updated_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return [TradeEpisode.model_validate_json(row["payload"]) for row in rows]

    def save_shadow_trial(self, trial: ShadowValidationTrial) -> None:
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
        bounded = min(max(limit, 1), 10000)
        terminal_statuses = {
            str(ShadowOutcomeStatus.NO_ENTRY),
            str(ShadowOutcomeStatus.CLOSED),
            str(ShadowOutcomeStatus.HORIZON_MARK),
        }
        clauses = ["(? IS NULL OR goal_id = ?)", "(? IS NULL OR symbol = ?)"]
        clauses.append("(? IS NULL OR timeframe = ?)")
        arguments: list = [
            goal_id,
            goal_id,
            symbol,
            symbol,
            timeframe,
            timeframe,
        ]
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
