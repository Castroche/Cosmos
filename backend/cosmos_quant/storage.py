from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from .configuration import UserConfiguration
from .strategy_spec import validate_strategy_spec
from .text_encoding import repair_text_tree


class SqliteStore:
    """Durable local state with WAL and explicit transactions."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def integrity_check(self) -> str:
        with self._lock, self.connect() as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"

    def backup_to(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                target.commit()
                result = target.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"backup integrity check failed: {result}")
            finally:
                target.close()
        return destination

    def _initialize(self) -> None:
        with self._lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_configuration (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_configuration (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_provider_runtime (
                    provider TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_usage_events (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_usage_events_created
                    ON ai_usage_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_usage_events_provider_created
                    ON ai_usage_events(provider, created_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_plans (
                    id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_candles (
                    exchange_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY (exchange_id, symbol, timeframe, timestamp)
                );
                CREATE INDEX IF NOT EXISTS idx_market_candles_lookup
                    ON market_candles(exchange_id, symbol, timeframe, timestamp DESC);
                CREATE TABLE IF NOT EXISTS paper_account_state (
                    account_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_backtests (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    strategy_fingerprint TEXT NOT NULL,
                    data_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_backtests_plan
                    ON strategy_backtests(plan_id, sequence DESC);
                CREATE TABLE IF NOT EXISTS research_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    data_fingerprint TEXT NOT NULL UNIQUE,
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_datasets_lookup
                    ON research_datasets(exchange_id, symbol, timeframe, created_at DESC);
                CREATE TABLE IF NOT EXISTS research_dataset_candles (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_funding (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    rate TEXT NOT NULL,
                    mark_price TEXT,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_open_interest (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    open_interest_amount TEXT,
                    open_interest_value TEXT,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_positioning (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    long_short_ratio TEXT NOT NULL,
                    long_account_fraction TEXT,
                    short_account_fraction TEXT,
                    source_variant TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_basis (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    mark_price TEXT NOT NULL,
                    index_price TEXT NOT NULL,
                    basis_rate TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_order_flow (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    aggregate_count INTEGER NOT NULL,
                    underlying_trade_count INTEGER NOT NULL,
                    base_volume TEXT NOT NULL,
                    quote_volume TEXT NOT NULL,
                    taker_buy_base_volume TEXT NOT NULL,
                    taker_sell_base_volume TEXT NOT NULL,
                    delta_base_volume TEXT NOT NULL,
                    maximum_trade_quote TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_dataset_liquidation_flow (
                    dataset_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (dataset_id, sequence),
                    FOREIGN KEY (dataset_id) REFERENCES research_datasets(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS liquidation_events (
                    event_id TEXT PRIMARY KEY,
                    exchange_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    collected_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_liquidation_events_symbol_time
                    ON liquidation_events(symbol, event_time);
                CREATE TABLE IF NOT EXISTS market_data_capture_sessions (
                    capture_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    sampling TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    ended_at TEXT,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_market_data_capture_scope
                    ON market_data_capture_sessions(source, stream, started_at);
                CREATE INDEX IF NOT EXISTS idx_market_data_capture_status
                    ON market_data_capture_sessions(status, last_heartbeat_at);
                CREATE TABLE IF NOT EXISTS market_catalog_journal_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    previous_snapshot_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(exchange_id, market_type, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_market_catalog_journal_scope
                    ON market_catalog_journal_snapshots(
                        exchange_id, market_type, observed_at DESC
                    );
                CREATE TABLE IF NOT EXISTS market_catalog_change_events (
                    event_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY (snapshot_id)
                        REFERENCES market_catalog_journal_snapshots(snapshot_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_market_catalog_events_scope
                    ON market_catalog_change_events(
                        exchange_id, market_type, observed_at DESC
                    );
                CREATE TABLE IF NOT EXISTS official_archive_indexes (
                    archive_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    source_payload_sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_official_archive_indexes_scope
                    ON official_archive_indexes(
                        exchange_id, market_type, retrieved_at DESC
                    );
                CREATE TABLE IF NOT EXISTS historical_anchor_scans (
                    scan_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    exchange_id TEXT NOT NULL,
                    anchor_month TEXT NOT NULL,
                    official_archive_index_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    FOREIGN KEY (official_archive_index_id)
                        REFERENCES official_archive_indexes(archive_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_historical_anchor_scans_scope
                    ON historical_anchor_scans(
                        exchange_id, anchor_month DESC, retrieved_at DESC
                    );
                CREATE TABLE IF NOT EXISTS research_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_jobs_plan
                    ON research_jobs(plan_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS research_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    selected_library_entry_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_campaigns_created
                    ON research_campaigns(created_at DESC);
                CREATE TABLE IF NOT EXISTS research_campaign_lineages (
                    lineage_id TEXT PRIMARY KEY,
                    blind_set_id TEXT,
                    status TEXT NOT NULL,
                    blind_consumption_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_campaign_trial_variants (
                    lineage_id TEXT NOT NULL,
                    strategy_fingerprint TEXT NOT NULL,
                    engine_version INTEGER NOT NULL,
                    first_experiment_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (lineage_id, strategy_fingerprint, engine_version),
                    FOREIGN KEY (lineage_id) REFERENCES research_campaign_lineages(lineage_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_campaign_experiments (
                    lineage_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    engine_version INTEGER NOT NULL,
                    research_configuration_fingerprint TEXT NOT NULL,
                    strategy_count INTEGER NOT NULL,
                    new_trial_variant_count INTEGER NOT NULL,
                    cumulative_hypothesis_count INTEGER NOT NULL,
                    sequential_experiment_count INTEGER NOT NULL,
                    effective_trial_count INTEGER NOT NULL,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_replayed_at TEXT,
                    PRIMARY KEY (lineage_id, experiment_id),
                    FOREIGN KEY (lineage_id) REFERENCES research_campaign_lineages(lineage_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_research_experiments_lineage_sequence
                    ON research_campaign_experiments(
                        lineage_id, sequential_experiment_count
                    );
                CREATE TABLE IF NOT EXISTS research_trial_families (
                    family_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_trial_family_variants (
                    family_id TEXT NOT NULL,
                    strategy_fingerprint TEXT NOT NULL,
                    engine_version INTEGER NOT NULL,
                    first_experiment_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (family_id, strategy_fingerprint, engine_version),
                    FOREIGN KEY (family_id) REFERENCES research_trial_families(family_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS research_trial_family_experiments (
                    family_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    engine_version INTEGER NOT NULL,
                    research_configuration_fingerprint TEXT NOT NULL,
                    strategy_count INTEGER NOT NULL,
                    new_trial_variant_count INTEGER NOT NULL,
                    cumulative_hypothesis_count INTEGER NOT NULL,
                    sequential_experiment_count INTEGER NOT NULL,
                    effective_trial_count INTEGER NOT NULL,
                    replay_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_replayed_at TEXT,
                    PRIMARY KEY (family_id, experiment_id),
                    FOREIGN KEY (family_id) REFERENCES research_trial_families(family_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_research_trial_family_sequence
                    ON research_trial_family_experiments(
                        family_id, sequential_experiment_count
                    );
                CREATE TABLE IF NOT EXISTS research_campaign_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_campaign_jobs_updated
                    ON research_campaign_jobs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_campaign_jobs_request
                    ON research_campaign_jobs(request_fingerprint, updated_at DESC);
                CREATE TABLE IF NOT EXISTS research_universe_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_universe_jobs_updated
                    ON research_universe_jobs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_research_universe_jobs_request
                    ON research_universe_jobs(request_fingerprint, updated_at DESC);
                CREATE TABLE IF NOT EXISTS research_universe_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_universe_snapshots_created
                    ON research_universe_snapshots(created_at DESC);
                CREATE TABLE IF NOT EXISTS research_campaign_cell_cache (
                    cache_key TEXT PRIMARY KEY,
                    strategy_fingerprint TEXT NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    engine_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_campaign_cell_strategy_dataset
                    ON research_campaign_cell_cache(
                        strategy_fingerprint, dataset_fingerprint
                    );
                CREATE TABLE IF NOT EXISTS blind_set_consumptions (
                    blind_set_id TEXT PRIMARY KEY,
                    strategy_fingerprint TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    campaign_id TEXT,
                    passed INTEGER,
                    opened_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS relative_value_reports (
                    report_id TEXT PRIMARY KEY,
                    strategy_fingerprint TEXT NOT NULL,
                    dataset_a_id TEXT NOT NULL,
                    dataset_b_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relative_value_created
                    ON relative_value_reports(created_at DESC);
                CREATE TABLE IF NOT EXISTS relative_value_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    engine_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_relative_value_campaign_created
                    ON relative_value_campaigns(created_at DESC);
                CREATE TABLE IF NOT EXISTS carry_reports (
                    report_id TEXT PRIMARY KEY,
                    strategy_fingerprint TEXT NOT NULL,
                    spot_dataset_id TEXT NOT NULL,
                    perpetual_dataset_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_carry_reports_created
                    ON carry_reports(created_at DESC);
                CREATE TABLE IF NOT EXISTS carry_campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    engine_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_carry_campaigns_created
                    ON carry_campaigns(created_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    spec_fingerprint TEXT NOT NULL,
                    family TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_candidates_status
                    ON strategy_candidates(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS risk_control_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS order_ledger (
                    exchange_id TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (exchange_id, order_id)
                );
                CREATE INDEX IF NOT EXISTS idx_order_ledger_client
                    ON order_ledger(exchange_id, client_order_id);
                CREATE TABLE IF NOT EXISTS multi_leg_execution_runs (
                    run_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_multi_leg_execution_status
                    ON multi_leg_execution_runs(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS multi_leg_commissioning (
                    exchange_id TEXT NOT NULL,
                    strategy_kind TEXT NOT NULL,
                    credential_fingerprint TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (exchange_id, strategy_kind)
                );
                CREATE TABLE IF NOT EXISTS account_daily_baseline (
                    account_id TEXT PRIMARY KEY,
                    session_date TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchange_commissioning (
                    exchange_id TEXT NOT NULL,
                    market_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (exchange_id, market_type)
                );
                CREATE TABLE IF NOT EXISTS agent_goals (
                    goal_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_policies (
                    policy_id TEXT PRIMARY KEY,
                    autonomy_mode TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    goal_id TEXT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    processed_at TEXT,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_pending
                    ON agent_events(status, goal_id, sequence);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    goal_id TEXT NOT NULL,
                    trigger_event_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (goal_id) REFERENCES agent_goals(goal_id) ON DELETE RESTRICT,
                    FOREIGN KEY (trigger_event_id) REFERENCES agent_events(event_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status
                    ON agent_runs(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS agent_decisions (
                    decision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_decisions_run
                    ON agent_decisions(run_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_observations (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool_call_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_observations_run
                    ON agent_observations(run_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_tool_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    result_payload TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_run
                    ON agent_tool_calls(run_id, created_at);
                CREATE TABLE IF NOT EXISTS agent_memories (
                    memory_id TEXT PRIMARY KEY,
                    memory_type TEXT NOT NULL,
                    source_episode_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memories_type
                    ON agent_memories(memory_type, created_at DESC);
                CREATE TABLE IF NOT EXISTS trade_episodes (
                    episode_id TEXT PRIMARY KEY,
                    plan_id TEXT,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trade_episodes_symbol
                    ON trade_episodes(symbol, updated_at DESC);
                CREATE TABLE IF NOT EXISTS agent_shadow_trials (
                    trial_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    decision_at TEXT NOT NULL,
                    horizon_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_shadow_trials_status
                    ON agent_shadow_trials(status, symbol, timeframe, updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_shadow_trials_plan
                    ON agent_shadow_trials(plan_id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(strategy_backtests)").fetchall()
            }
            if "dataset_id" not in columns:
                connection.execute("ALTER TABLE strategy_backtests ADD COLUMN dataset_id TEXT")
            candidate_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(strategy_candidates)").fetchall()
            }
            if "family" not in candidate_columns:
                connection.execute(
                    "ALTER TABLE strategy_candidates ADD COLUMN family TEXT NOT NULL DEFAULT ''"
                )
                rows = connection.execute(
                    "SELECT candidate_id, payload FROM strategy_candidates"
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["payload"])
                    connection.execute(
                        "UPDATE strategy_candidates SET family = ? WHERE candidate_id = ?",
                        (payload.get("spec", {}).get("family", ""), row["candidate_id"]),
                    )
            agent_event_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(agent_events)").fetchall()
            }
            if "goal_id" not in agent_event_columns:
                connection.execute("ALTER TABLE agent_events ADD COLUMN goal_id TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_candidates_champion
                ON strategy_candidates(family) WHERE status = 'champion'
                """
            )

    def load_configuration(self) -> tuple[UserConfiguration, datetime | None]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload, updated_at FROM app_configuration WHERE id = 1"
            ).fetchone()
        if not row:
            return UserConfiguration(), None
        payload = json.loads(row["payload"])
        repaired = repair_text_tree(payload)
        if repaired != payload:
            with self._lock, self.connect() as connection:
                connection.execute(
                    "UPDATE app_configuration SET payload = ? WHERE id = 1",
                    (json.dumps(repaired, ensure_ascii=False),),
                )
        return (
            UserConfiguration.model_validate(repaired),
            datetime.fromisoformat(row["updated_at"]),
        )

    def save_configuration(self, config: UserConfiguration) -> datetime:
        now = datetime.now(UTC)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO app_configuration (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (config.model_dump_json(), now.isoformat()),
            )
            connection.commit()
        return now

    def load_ai_configuration(self) -> tuple[dict | None, datetime | None]:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload, updated_at FROM ai_configuration WHERE id = 1"
            ).fetchone()
        if not row:
            return None, None
        return json.loads(row["payload"]), datetime.fromisoformat(row["updated_at"])

    def save_ai_configuration(self, config) -> datetime:
        now = datetime.now(UTC)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ai_configuration (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (config.model_dump_json(), now.isoformat()),
            )
            connection.commit()
        return now

    def save_ai_provider_runtime(self, provider, state) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_provider_runtime (provider, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (str(provider), state.model_dump_json(), now),
            )

    def load_ai_provider_runtime(self, provider) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM ai_provider_runtime WHERE provider = ?",
                (str(provider),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_ai_usage_event(self, event) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_usage_events
                    (id, provider, model, operation, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    str(event.provider),
                    event.model,
                    event.operation,
                    event.model_dump_json(),
                    event.created_at.isoformat(),
                ),
            )

    def load_ai_usage_events(
        self,
        *,
        since: datetime,
        until: datetime,
        provider=None,
        limit: int | None = None,
    ) -> list[dict]:
        clauses = ["created_at >= ?", "created_at <= ?"]
        parameters: list[object] = [since.isoformat(), until.isoformat()]
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(str(provider))
        query = (
            "SELECT payload FROM ai_usage_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC"
        )
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock, self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_commissioning(self, exchange_id: str, market_type: str, result) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO exchange_commissioning
                    (exchange_id, market_type, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(exchange_id, market_type) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    exchange_id,
                    market_type,
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_commissioning(self, exchange_id: str, market_type: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM exchange_commissioning
                WHERE exchange_id = ? AND market_type = ?
                """,
                (exchange_id, market_type),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_strategy(self, plan) -> None:
        now = datetime.now(UTC).isoformat()
        payload = plan.model_dump_json()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO strategy_plans (id, version, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    plan.id,
                    plan.version,
                    str(plan.status),
                    payload,
                    plan.created_at.isoformat(),
                    now,
                ),
            )
            connection.commit()

    def load_strategies(self) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM strategy_plans ORDER BY updated_at DESC"
            ).fetchall()
        strategies = []
        for row in rows:
            payload = json.loads(row["payload"])
            repaired = repair_text_tree(payload)
            strategies.append(repaired)
            if repaired != payload:
                with self._lock, self.connect() as connection:
                    connection.execute(
                        "UPDATE strategy_plans SET payload = ? WHERE id = ?",
                        (json.dumps(repaired, ensure_ascii=False), repaired["id"]),
                    )
        return strategies

    def save_backtest(self, plan_id: str, result) -> None:
        if not result.strategy_fingerprint or not result.data_fingerprint:
            raise ValueError("backtest provenance is required")
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_backtests
                    (plan_id, strategy_fingerprint, data_fingerprint, dataset_id,
                     payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    result.strategy_fingerprint,
                    result.data_fingerprint,
                    result.dataset_id,
                    result.model_dump_json(),
                    result.created_at.isoformat(),
                ),
            )

    def save_research_dataset(
        self,
        dataset,
        candles,
        funding_rates=None,
        open_interest=None,
        positioning=None,
        basis=None,
        order_flow=None,
        liquidation_flow=None,
    ) -> None:
        if len(candles) != dataset.candle_count:
            raise ValueError("dataset candle count does not match payload")
        if len(funding_rates or []) != dataset.funding_count:
            raise ValueError("dataset funding count does not match payload")
        if len(open_interest or []) != dataset.open_interest_count:
            raise ValueError("dataset open-interest count does not match payload")
        if len(positioning or []) != dataset.positioning_count:
            raise ValueError("dataset positioning count does not match payload")
        if len(basis or []) != dataset.basis_count:
            raise ValueError("dataset basis count does not match payload")
        if len(order_flow or []) != dataset.order_flow_count:
            raise ValueError("dataset order-flow count does not match payload")
        if len(liquidation_flow or []) != dataset.liquidation_flow_count:
            raise ValueError("dataset liquidation-flow count does not match payload")
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM research_datasets WHERE dataset_id = ?",
                (dataset.id,),
            ).fetchone()
            if existing:
                if json.loads(existing["payload"])["data_fingerprint"] != dataset.data_fingerprint:
                    raise RuntimeError("immutable research dataset id collision")
                connection.commit()
                return
            connection.execute(
                """
                INSERT INTO research_datasets
                    (dataset_id, data_fingerprint, exchange_id, market_type,
                     symbol, timeframe, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.id,
                    dataset.data_fingerprint,
                    dataset.exchange,
                    dataset.market_type,
                    dataset.symbol,
                    dataset.timeframe,
                    dataset.model_dump_json(),
                    dataset.created_at.isoformat(),
                ),
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_candles
                    (dataset_id, sequence, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        candle.timestamp.isoformat(),
                        str(candle.open),
                        str(candle.high),
                        str(candle.low),
                        str(candle.close),
                        str(candle.volume),
                    )
                    for index, candle in enumerate(candles)
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_funding
                    (dataset_id, sequence, timestamp, rate, mark_price)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        str(point.rate),
                        str(point.mark_price) if point.mark_price is not None else None,
                    )
                    for index, point in enumerate(funding_rates or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_open_interest
                    (dataset_id, sequence, timestamp,
                     open_interest_amount, open_interest_value)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        (
                            str(point.open_interest_amount)
                            if point.open_interest_amount is not None
                            else None
                        ),
                        (
                            str(point.open_interest_value)
                            if point.open_interest_value is not None
                            else None
                        ),
                    )
                    for index, point in enumerate(open_interest or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_positioning
                    (dataset_id, sequence, timestamp, long_short_ratio,
                     long_account_fraction, short_account_fraction, source_variant)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        str(point.long_short_ratio),
                        (
                            str(point.long_account_fraction)
                            if point.long_account_fraction is not None
                            else None
                        ),
                        (
                            str(point.short_account_fraction)
                            if point.short_account_fraction is not None
                            else None
                        ),
                        point.source_variant,
                    )
                    for index, point in enumerate(positioning or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_basis
                    (dataset_id, sequence, timestamp, mark_price, index_price, basis_rate)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        str(point.mark_price),
                        str(point.index_price),
                        str(point.basis_rate),
                    )
                    for index, point in enumerate(basis or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_order_flow
                    (dataset_id, sequence, timestamp, aggregate_count,
                     underlying_trade_count, base_volume, quote_volume,
                     taker_buy_base_volume, taker_sell_base_volume,
                     delta_base_volume, maximum_trade_quote)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        point.aggregate_count,
                        point.underlying_trade_count,
                        str(point.base_volume),
                        str(point.quote_volume),
                        str(point.taker_buy_base_volume),
                        str(point.taker_sell_base_volume),
                        str(point.delta_base_volume),
                        str(point.maximum_trade_quote),
                    )
                    for index, point in enumerate(order_flow or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_liquidation_flow
                    (dataset_id, sequence, timestamp, payload)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        point.model_dump_json(),
                    )
                    for index, point in enumerate(liquidation_flow or [])
                ],
            )
            connection.commit()

    def load_research_dataset(self, dataset_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_research_dataset_candles(self, dataset_id: str) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM research_dataset_candles
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_funding(self, dataset_id: str) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, rate, mark_price
                FROM research_dataset_funding
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_open_interest(
        self,
        dataset_id: str,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, open_interest_amount, open_interest_value
                FROM research_dataset_open_interest
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_positioning(
        self,
        dataset_id: str,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, long_short_ratio, long_account_fraction,
                       short_account_fraction, source_variant
                FROM research_dataset_positioning
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_basis(
        self,
        dataset_id: str,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, mark_price, index_price, basis_rate
                FROM research_dataset_basis
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_order_flow(
        self,
        dataset_id: str,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, aggregate_count, underlying_trade_count,
                       base_volume, quote_volume, taker_buy_base_volume,
                       taker_sell_base_volume, delta_base_volume,
                       maximum_trade_quote
                FROM research_dataset_order_flow
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_research_dataset_liquidation_flow(
        self,
        dataset_id: str,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_dataset_liquidation_flow
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_liquidation_event(self, event) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO liquidation_events
                    (event_id, exchange_id, symbol, event_time, payload, collected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.exchange,
                    event.symbol,
                    event.event_time.isoformat(),
                    event.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def list_liquidation_events(
        self,
        *,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        bounded = min(max(limit, 1), 10000)
        with self._lock, self.connect() as connection:
            if symbol:
                rows = connection.execute(
                    """
                    SELECT payload FROM liquidation_events
                    WHERE symbol = ? ORDER BY event_time DESC LIMIT ?
                    """,
                    (symbol, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT payload FROM liquidation_events
                    ORDER BY event_time DESC LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def summarize_liquidation_events(self) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT exchange_id AS exchange, symbol, COUNT(*) AS point_count,
                       MIN(event_time) AS first_event_at,
                       MAX(event_time) AS last_event_at
                FROM liquidation_events
                GROUP BY exchange_id, symbol
                ORDER BY exchange_id, symbol
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def start_market_data_capture_session(self, session) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO market_data_capture_sessions
                    (capture_id, source, stream, symbols_json, sampling, status,
                     started_at, last_heartbeat_at, ended_at, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.source,
                    session.stream,
                    json.dumps(sorted(set(session.symbols)), ensure_ascii=False),
                    session.sampling,
                    str(session.status),
                    session.started_at.isoformat(),
                    session.last_heartbeat_at.isoformat(),
                    session.ended_at.isoformat() if session.ended_at else None,
                    session.error,
                    now,
                    now,
                ),
            )

    def heartbeat_market_data_capture_session(
        self,
        capture_id: str,
        heartbeat_at: datetime,
    ) -> bool:
        timestamp = heartbeat_at.isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE market_data_capture_sessions
                SET last_heartbeat_at = ?, updated_at = ?
                WHERE capture_id = ? AND status = 'connected'
                  AND last_heartbeat_at <= ?
                """,
                (timestamp, timestamp, capture_id, timestamp),
            )
        return cursor.rowcount == 1

    def finish_market_data_capture_session(
        self,
        capture_id: str,
        *,
        status: str,
        ended_at: datetime,
        error: str = "",
    ) -> bool:
        if status == "connected":
            raise ValueError("terminal capture status cannot be connected")
        timestamp = ended_at.isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE market_data_capture_sessions
                SET status = ?, ended_at = ?,
                    last_heartbeat_at = CASE
                        WHEN last_heartbeat_at < ? THEN ? ELSE last_heartbeat_at END,
                    error = ?, updated_at = ?
                WHERE capture_id = ? AND status = 'connected'
                """,
                (status, timestamp, timestamp, timestamp, error[:500], timestamp, capture_id),
            )
        return cursor.rowcount == 1

    def interrupt_open_market_data_capture_sessions(
        self,
        *,
        source: str,
        stream: str,
        reason: str,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE market_data_capture_sessions
                SET status = 'interrupted', ended_at = last_heartbeat_at,
                    error = ?, updated_at = ?
                WHERE source = ? AND stream = ? AND status = 'connected'
                """,
                (reason[:500], now, source, stream),
            )
        return cursor.rowcount

    def list_market_data_capture_sessions(
        self,
        *,
        source: str | None = None,
        stream: str | None = None,
        limit: int = 100000,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if source:
            clauses.append("source = ?")
            parameters.append(source)
        if stream:
            clauses.append("stream = ?")
            parameters.append(stream)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded = min(max(limit, 1), 500000)
        parameters.append(bounded)
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT capture_id AS id, source, stream, symbols_json, sampling,
                       status, started_at, last_heartbeat_at, ended_at, error
                FROM market_data_capture_sessions
                {where}
                ORDER BY started_at ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            {
                **dict(row),
                "symbols": json.loads(row["symbols_json"]),
            }
            for row in rows
        ]

    def append_market_catalog_journal_observation(
        self,
        snapshot,
        events: list,
    ) -> None:
        with self._lock, self.connect() as connection:
            latest = connection.execute(
                """
                SELECT snapshot_id FROM market_catalog_journal_snapshots
                WHERE exchange_id = ? AND market_type = ?
                ORDER BY observed_at DESC LIMIT 1
                """,
                (snapshot.exchange, snapshot.market_type),
            ).fetchone()
            latest_id = latest["snapshot_id"] if latest else None
            if latest_id != snapshot.previous_snapshot_id:
                raise RuntimeError(
                    "catalog journal append would fork or skip the durable chain"
                )
            connection.execute(
                """
                INSERT INTO market_catalog_journal_snapshots
                    (snapshot_id, exchange_id, market_type, observed_at,
                     previous_snapshot_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.exchange,
                    snapshot.market_type,
                    snapshot.observed_at.isoformat(),
                    snapshot.previous_snapshot_id,
                    snapshot.model_dump_json(),
                    snapshot.created_at.isoformat(),
                ),
            )
            for event in events:
                if event.snapshot_id != snapshot.id:
                    raise RuntimeError("catalog event is not bound to its appended snapshot")
                connection.execute(
                    """
                    INSERT INTO market_catalog_change_events
                        (event_id, snapshot_id, exchange_id, market_type, market_id,
                         change_type, observed_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.snapshot_id,
                        event.exchange,
                        event.market_type,
                        event.market_id,
                        event.change_type,
                        event.observed_at.isoformat(),
                        event.model_dump_json(),
                    ),
                )

    def load_latest_market_catalog_journal_snapshot(
        self,
        *,
        exchange: str,
        market_type: str,
    ) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM market_catalog_journal_snapshots
                WHERE exchange_id = ? AND market_type = ?
                ORDER BY observed_at DESC LIMIT 1
                """,
                (exchange, market_type),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_market_catalog_journal_snapshots(
        self,
        *,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if exchange:
            clauses.append("exchange_id = ?")
            parameters.append(exchange)
        if market_type:
            clauses.append("market_type = ?")
            parameters.append(market_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(min(max(limit, 1), 100000))
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM market_catalog_journal_snapshots {where}
                ORDER BY observed_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_market_catalog_change_events(
        self,
        *,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if exchange:
            clauses.append("exchange_id = ?")
            parameters.append(exchange)
        if market_type:
            clauses.append("market_type = ?")
            parameters.append(market_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(min(max(limit, 1), 100000))
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM market_catalog_change_events {where}
                ORDER BY observed_at DESC, event_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_official_archive_index(self, archive) -> None:
        """Persist a server-fetched official archive index idempotently by fingerprint."""

        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO official_archive_indexes
                    (archive_id, fingerprint, exchange_id, market_type,
                     source_payload_sha256, payload, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    archive.id,
                    archive.fingerprint,
                    archive.exchange,
                    archive.market_type,
                    archive.source_payload_sha256,
                    archive.model_dump_json(),
                    archive.retrieved_at.isoformat(),
                ),
            )

    def load_official_archive_index(self, archive_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM official_archive_indexes WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_official_archive_indexes(
        self,
        *,
        exchange: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses: list[str] = []
        parameters: list[object] = []
        if exchange:
            clauses.append("exchange_id = ?")
            parameters.append(exchange)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(min(max(limit, 1), 1000))
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM official_archive_indexes {where}
                ORDER BY retrieved_at DESC, archive_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_historical_anchor_scan(self, scan) -> None:
        with self._lock, self.connect() as connection:
            archive = connection.execute(
                "SELECT fingerprint FROM official_archive_indexes WHERE archive_id = ?",
                (scan.official_archive_index_id,),
            ).fetchone()
            if archive is None:
                raise RuntimeError("historical anchor scan references an unknown archive index")
            if archive["fingerprint"] != scan.official_archive_index_fingerprint:
                raise RuntimeError("historical anchor scan archive fingerprint mismatch")
            connection.execute(
                """
                INSERT INTO historical_anchor_scans
                    (scan_id, fingerprint, exchange_id, anchor_month,
                     official_archive_index_id, payload, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (
                    scan.id,
                    scan.fingerprint,
                    scan.exchange,
                    scan.anchor_month.isoformat(),
                    scan.official_archive_index_id,
                    scan.model_dump_json(),
                    scan.retrieved_at.isoformat(),
                ),
            )

    def load_historical_anchor_scan(self, scan_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM historical_anchor_scans WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_historical_anchor_scans(
        self,
        *,
        exchange: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        parameters: list[object] = []
        where = ""
        if exchange:
            where = "WHERE exchange_id = ?"
            parameters.append(exchange)
        parameters.append(min(max(limit, 1), 1000))
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM historical_anchor_scans {where}
                ORDER BY anchor_month DESC, retrieved_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_research_datasets(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_datasets
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_job(self, job) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_jobs (job_id, plan_id, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.plan_id,
                    str(job.status),
                    job.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_research_job(self, job_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_jobs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_campaign(self, campaign) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_campaigns
                    (campaign_id, status, selected_library_entry_id,
                     payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    status = excluded.status,
                    selected_library_entry_id =
                        excluded.selected_library_entry_id,
                    payload = excluded.payload
                """,
                (
                    campaign.id,
                    campaign.status,
                    campaign.selected_library_entry_id,
                    campaign.model_dump_json(),
                    campaign.created_at.isoformat(),
                ),
            )

    def load_research_campaign(self, campaign_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_campaigns(self, limit: int = 20) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_campaigns
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def register_research_trial_family_experiment(
        self,
        *,
        family_id: str,
        experiment_id: str,
        campaign_id: str,
        engine_version: int,
        research_configuration_fingerprint: str,
        strategy_fingerprints: list[str],
    ) -> dict:
        """Charge trials across refreshed downloads and evidence lineages."""

        fingerprints = sorted(set(strategy_fingerprints))
        if not fingerprints:
            raise ValueError("research trial family requires strategy fingerprints")
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO research_trial_families (family_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(family_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (family_id, now, now),
            )
            existing = connection.execute(
                """
                SELECT * FROM research_trial_family_experiments
                WHERE family_id = ? AND experiment_id = ?
                """,
                (family_id, experiment_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE research_trial_family_experiments
                    SET replay_count = replay_count + 1, last_replayed_at = ?
                    WHERE family_id = ? AND experiment_id = ?
                    """,
                    (now, family_id, experiment_id),
                )
                connection.commit()
                payload = dict(existing)
                payload["trial_family_id"] = family_id
                # A replay must retain the multiplicity budget frozen when this
                # experiment was first registered. Later experiments may grow
                # the family ledger, but retroactively rewriting an old report
                # breaks its immutable audit contract and cache reproducibility.
                payload["replayed"] = True
                return payload
            new_trial_variant_count = 0
            for fingerprint in fingerprints:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO research_trial_family_variants
                        (family_id, strategy_fingerprint, engine_version,
                         first_experiment_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (family_id, fingerprint, engine_version, experiment_id, now),
                )
                new_trial_variant_count += max(cursor.rowcount, 0)
            cumulative_hypothesis_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM research_trial_family_variants
                    WHERE family_id = ?
                    """,
                    (family_id,),
                ).fetchone()[0]
            )
            sequential_experiment_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) + 1 FROM research_trial_family_experiments
                    WHERE family_id = ?
                    """,
                    (family_id,),
                ).fetchone()[0]
            )
            effective_trial_count = (
                cumulative_hypothesis_count + sequential_experiment_count - 1
            )
            connection.execute(
                """
                INSERT INTO research_trial_family_experiments
                    (family_id, experiment_id, campaign_id, engine_version,
                     research_configuration_fingerprint, strategy_count,
                     new_trial_variant_count, cumulative_hypothesis_count,
                     sequential_experiment_count, effective_trial_count,
                     replay_count, created_at, last_replayed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)
                """,
                (
                    family_id,
                    experiment_id,
                    campaign_id,
                    engine_version,
                    research_configuration_fingerprint,
                    len(fingerprints),
                    new_trial_variant_count,
                    cumulative_hypothesis_count,
                    sequential_experiment_count,
                    effective_trial_count,
                    now,
                ),
            )
            connection.commit()
        return {
            "trial_family_id": family_id,
            "experiment_id": experiment_id,
            "campaign_id": campaign_id,
            "engine_version": engine_version,
            "research_configuration_fingerprint": research_configuration_fingerprint,
            "strategy_count": len(fingerprints),
            "new_trial_variant_count": new_trial_variant_count,
            "cumulative_hypothesis_count": cumulative_hypothesis_count,
            "sequential_experiment_count": sequential_experiment_count,
            "effective_trial_count": effective_trial_count,
            "replay_count": 0,
            "replayed": False,
        }

    def load_research_trial_family_experiment(
        self,
        *,
        family_id: str,
        experiment_id: str,
    ) -> dict | None:
        """Read the immutable cross-download multiplicity evidence for one experiment."""

        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM research_trial_family_experiments
                WHERE family_id = ? AND experiment_id = ?
                """,
                (family_id, experiment_id),
            ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["trial_family_id"] = family_id
        return payload

    def register_research_campaign_experiment(
        self,
        *,
        lineage_id: str,
        experiment_id: str,
        campaign_id: str,
        engine_version: int,
        research_configuration_fingerprint: str,
        strategy_fingerprints: list[str],
        blind_set_id: str | None = None,
        trial_family_id: str | None = None,
    ) -> dict:
        """Atomically charge one unique experiment to a permanent data lineage."""

        fingerprints = sorted(set(strategy_fingerprints))
        if not fingerprints:
            raise ValueError("research experiment requires at least one strategy fingerprint")
        family_summary = None
        if trial_family_id:
            family_summary = self.register_research_trial_family_experiment(
                family_id=trial_family_id,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                engine_version=engine_version,
                research_configuration_fingerprint=research_configuration_fingerprint,
                strategy_fingerprints=fingerprints,
            )
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM research_campaign_experiments
                WHERE lineage_id = ? AND experiment_id = ?
                """,
                (lineage_id, experiment_id),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    """
                    UPDATE research_campaign_experiments
                    SET replay_count = replay_count + 1,
                        last_replayed_at = ?
                    WHERE lineage_id = ? AND experiment_id = ?
                    """,
                    (now, lineage_id, experiment_id),
                )
                connection.commit()
                payload = dict(existing)
                if family_summary is not None:
                    payload.update(family_summary)
                    payload["lineage_id"] = lineage_id
                payload["replayed"] = True
                return payload
            connection.execute(
                """
                INSERT INTO research_campaign_lineages
                    (lineage_id, blind_set_id, status, blind_consumption_id,
                     created_at, updated_at)
                VALUES (?, ?, 'open', NULL, ?, ?)
                ON CONFLICT(lineage_id) DO UPDATE SET
                    blind_set_id = COALESCE(
                        research_campaign_lineages.blind_set_id,
                        excluded.blind_set_id
                    ),
                    updated_at = excluded.updated_at
                """,
                (lineage_id, blind_set_id, now, now),
            )
            lineage = connection.execute(
                "SELECT * FROM research_campaign_lineages WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
            if (
                lineage is not None
                and lineage["blind_set_id"] is not None
                and blind_set_id is not None
                and lineage["blind_set_id"] != blind_set_id
            ):
                connection.rollback()
                raise RuntimeError("research lineage is already bound to a different blind set")
            if blind_set_id:
                blind_consumption = connection.execute(
                    """
                    SELECT evaluation_id FROM blind_set_consumptions
                    WHERE blind_set_id = ?
                    """,
                    (blind_set_id,),
                ).fetchone()
                if blind_consumption is not None:
                    connection.execute(
                        """
                        UPDATE research_campaign_lineages
                        SET status = 'blind_opened', blind_consumption_id = ?,
                            updated_at = ?
                        WHERE lineage_id = ?
                        """,
                        (blind_consumption["evaluation_id"], now, lineage_id),
                    )
                    connection.commit()
                    raise RuntimeError("research lineage is sealed after blind-set opening")
            if lineage is None or lineage["status"] != "open":
                connection.rollback()
                raise RuntimeError("research lineage is sealed after blind-set opening")
            new_trial_variant_count = 0
            for fingerprint in fingerprints:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO research_campaign_trial_variants
                        (lineage_id, strategy_fingerprint, engine_version,
                         first_experiment_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        lineage_id,
                        fingerprint,
                        engine_version,
                        experiment_id,
                        now,
                    ),
                )
                new_trial_variant_count += max(cursor.rowcount, 0)
            cumulative_hypothesis_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM research_campaign_trial_variants
                    WHERE lineage_id = ?
                    """,
                    (lineage_id,),
                ).fetchone()[0]
            )
            sequential_experiment_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) + 1 FROM research_campaign_experiments
                    WHERE lineage_id = ?
                    """,
                    (lineage_id,),
                ).fetchone()[0]
            )
            effective_trial_count = cumulative_hypothesis_count + sequential_experiment_count - 1
            connection.execute(
                """
                INSERT INTO research_campaign_experiments
                    (lineage_id, experiment_id, campaign_id, engine_version,
                     research_configuration_fingerprint, strategy_count,
                     new_trial_variant_count, cumulative_hypothesis_count,
                     sequential_experiment_count, effective_trial_count,
                     replay_count, created_at, last_replayed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL)
                """,
                (
                    lineage_id,
                    experiment_id,
                    campaign_id,
                    engine_version,
                    research_configuration_fingerprint,
                    len(fingerprints),
                    new_trial_variant_count,
                    cumulative_hypothesis_count,
                    sequential_experiment_count,
                    effective_trial_count,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE research_campaign_lineages SET updated_at = ?
                WHERE lineage_id = ?
                """,
                (now, lineage_id),
            )
            connection.commit()
        result = {
            "lineage_id": lineage_id,
            "experiment_id": experiment_id,
            "campaign_id": campaign_id,
            "engine_version": engine_version,
            "research_configuration_fingerprint": (research_configuration_fingerprint),
            "strategy_count": len(fingerprints),
            "new_trial_variant_count": new_trial_variant_count,
            "cumulative_hypothesis_count": cumulative_hypothesis_count,
            "sequential_experiment_count": sequential_experiment_count,
            "effective_trial_count": effective_trial_count,
            "replay_count": 0,
            "replayed": False,
        }
        if family_summary is not None:
            result.update(family_summary)
            result["lineage_id"] = lineage_id
        return result

    def mark_research_lineage_blind_opened(
        self,
        *,
        lineage_id: str,
        blind_consumption_id: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM research_campaign_lineages WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("research lineage is missing")
            existing_consumption = row["blind_consumption_id"]
            if existing_consumption not in {None, blind_consumption_id}:
                connection.rollback()
                raise RuntimeError("research lineage already consumed another blind set")
            connection.execute(
                """
                UPDATE research_campaign_lineages
                SET status = 'blind_opened', blind_consumption_id = ?, updated_at = ?
                WHERE lineage_id = ?
                """,
                (blind_consumption_id, now, lineage_id),
            )
            connection.commit()

    def load_research_campaign_lineage(self, lineage_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_campaign_lineages WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_research_campaign_experiments(self, lineage_id: str) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM research_campaign_experiments
                WHERE lineage_id = ?
                ORDER BY sequential_experiment_count
                """,
                (lineage_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_research_campaign_job(self, job) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_campaign_jobs
                    (job_id, request_fingerprint, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    request_fingerprint = excluded.request_fingerprint,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.request_fingerprint,
                    str(job.status),
                    job.model_dump_json(),
                    job.updated_at.isoformat(),
                ),
            )

    def load_research_campaign_job(self, job_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_campaign_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_campaign_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_campaign_jobs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_universe_job(self, job) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_universe_jobs
                    (job_id, request_fingerprint, status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    request_fingerprint = excluded.request_fingerprint,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.request_fingerprint,
                    str(job.status),
                    job.model_dump_json(),
                    job.updated_at.isoformat(),
                ),
            )

    def load_research_universe_job(self, job_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_universe_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_universe_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_universe_jobs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_universe_snapshot(self, snapshot) -> None:
        payload = snapshot.model_dump_json()
        with self._lock, self.connect() as connection:
            existing = connection.execute(
                "SELECT payload FROM research_universe_snapshots WHERE snapshot_id = ?",
                (snapshot.id,),
            ).fetchone()
            if existing is not None and existing["payload"] != payload:
                raise ValueError("immutable research universe snapshot collision")
            connection.execute(
                """
                INSERT OR IGNORE INTO research_universe_snapshots
                    (snapshot_id, fingerprint, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.fingerprint,
                    payload,
                    snapshot.created_at.isoformat(),
                ),
            )

    def load_research_universe_snapshot(self, snapshot_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_universe_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_universe_snapshots(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_universe_snapshots
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def load_research_campaign_cell(self, cache_key: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM research_campaign_cell_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_research_campaign_cell(
        self,
        *,
        cache_key: str,
        strategy_fingerprint: str,
        dataset_fingerprint: str,
        engine_version: int,
        cell,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_campaign_cell_cache
                    (cache_key, strategy_fingerprint, dataset_fingerprint,
                     engine_version, payload, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    last_used_at = excluded.last_used_at
                """,
                (
                    cache_key,
                    strategy_fingerprint,
                    dataset_fingerprint,
                    engine_version,
                    cell.model_dump_json(),
                    now,
                    now,
                ),
            )

    def reserve_blind_set(
        self,
        *,
        blind_set_id: str,
        strategy_fingerprint: str,
        evaluation_id: str,
    ) -> bool:
        """Atomically break a one-time blind seal before any holdout data is loaded."""

        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO blind_set_consumptions
                    (blind_set_id, strategy_fingerprint, evaluation_id,
                     status, opened_at)
                VALUES (?, ?, ?, 'opened', ?)
                """,
                (
                    blind_set_id,
                    strategy_fingerprint,
                    evaluation_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def complete_blind_set_consumption(
        self,
        *,
        evaluation_id: str,
        campaign_id: str,
        passed: bool,
    ) -> None:
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE blind_set_consumptions
                SET status = 'completed',
                    campaign_id = ?,
                    passed = ?,
                    completed_at = ?
                WHERE evaluation_id = ? AND status = 'opened'
                """,
                (
                    campaign_id,
                    int(passed),
                    datetime.now(UTC).isoformat(),
                    evaluation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("blind-set consumption is missing or already completed")

    def load_blind_set_consumption(self, blind_set_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT blind_set_id, strategy_fingerprint, evaluation_id,
                       status, campaign_id, passed, opened_at, completed_at
                FROM blind_set_consumptions
                WHERE blind_set_id = ?
                """,
                (blind_set_id,),
            ).fetchone()
        return dict(row) if row else None

    def save_relative_value_report(self, report) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO relative_value_reports
                    (report_id, strategy_fingerprint, dataset_a_id,
                     dataset_b_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    report.id,
                    report.strategy_fingerprint,
                    report.dataset_a_id,
                    report.dataset_b_id,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )

    def load_relative_value_report(self, report_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM relative_value_reports
                WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_relative_value_reports(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM relative_value_reports
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_relative_value_campaign(self, report) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO relative_value_campaigns
                    (campaign_id, engine_version, payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    report.id,
                    report.engine_version,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )

    def load_relative_value_campaign(self, campaign_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM relative_value_campaigns
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_relative_value_campaigns(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM relative_value_campaigns
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_carry_report(self, report) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO carry_reports
                    (report_id, strategy_fingerprint, spot_dataset_id,
                     perpetual_dataset_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    report.id,
                    report.strategy_fingerprint,
                    report.spot_dataset_id,
                    report.perpetual_dataset_id,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )

    def load_carry_report(self, report_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM carry_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_carry_reports(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM carry_reports
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_carry_campaign(self, report) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO carry_campaigns
                    (campaign_id, engine_version, payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(campaign_id) DO UPDATE SET payload = excluded.payload
                """,
                (
                    report.id,
                    report.engine_version,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )

    def load_carry_campaign(self, campaign_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM carry_campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_carry_campaigns(self, limit: int = 50) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM carry_campaigns
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_strategy_candidate(self, candidate) -> None:
        actual_fingerprint = validate_strategy_spec(candidate.spec).fingerprint
        if actual_fingerprint != candidate.spec_fingerprint:
            raise ValueError("strategy candidate fingerprint does not match payload")
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT spec_fingerprint FROM strategy_candidates
                WHERE candidate_id = ?
                """,
                (candidate.id,),
            ).fetchone()
            if existing and existing["spec_fingerprint"] != candidate.spec_fingerprint:
                raise RuntimeError("strategy candidate specification is immutable")
            connection.execute(
                """
                INSERT INTO strategy_candidates
                    (candidate_id, spec_fingerprint, family, status, source,
                     payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    candidate.id,
                    candidate.spec_fingerprint,
                    candidate.spec["family"],
                    str(candidate.status),
                    candidate.source,
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                ),
            )
            connection.commit()

    def promote_strategy_candidate(
        self,
        candidate,
        retired_candidate=None,
    ) -> None:
        family = candidate.spec["family"]
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT candidate_id FROM strategy_candidates
                WHERE family = ? AND status = 'champion'
                """,
                (family,),
            ).fetchone()
            expected_id = retired_candidate.id if retired_candidate else None
            current_id = current["candidate_id"] if current else None
            if current_id != expected_id:
                connection.rollback()
                raise RuntimeError("Champion changed during promotion; retry review")
            stored = connection.execute(
                """
                SELECT status, spec_fingerprint FROM strategy_candidates
                WHERE candidate_id = ?
                """,
                (candidate.id,),
            ).fetchone()
            if (
                not stored
                or stored["status"] != "challenger"
                or stored["spec_fingerprint"] != candidate.spec_fingerprint
            ):
                connection.rollback()
                raise RuntimeError("candidate is no longer an eligible Challenger")
            if retired_candidate:
                connection.execute(
                    """
                    UPDATE strategy_candidates
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        str(retired_candidate.status),
                        retired_candidate.model_dump_json(),
                        retired_candidate.updated_at.isoformat(),
                        retired_candidate.id,
                    ),
                )
            connection.execute(
                """
                UPDATE strategy_candidates
                SET status = ?, payload = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (
                    str(candidate.status),
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                    candidate.id,
                ),
            )
            connection.commit()

    def load_strategy_candidate(self, candidate_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM strategy_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_strategy_candidates(self, limit: int = 100) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM strategy_candidates
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def retire_superseded_system_candidate(self, candidate_id: str) -> bool:
        """Archive an obsolete built-in even when its legacy spec no longer validates."""

        reasons = [
            "该内置模板版本已被当前公开策略库替代并自动归档",
            "历史候选仅保留审计记录，不再参与研究排序或执行",
        ]
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT source, payload FROM strategy_candidates
                WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if not row or row["source"] != "system":
                connection.rollback()
                return False
            payload = repair_text_tree(json.loads(row["payload"]))
            payload["status"] = "retired"
            payload["execution_artifact"] = None
            payload["decision_reasons"] = reasons
            payload["updated_at"] = now
            connection.execute(
                """
                UPDATE strategy_candidates
                SET status = 'retired', payload = ?, updated_at = ?
                WHERE candidate_id = ? AND source = 'system'
                """,
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    candidate_id,
                ),
            )
            connection.commit()
        return True

    def load_latest_backtest(self, plan_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM strategy_backtests
                WHERE plan_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_risk_state(self, payload: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO risk_control_state (id, payload, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (payload, datetime.now(UTC).isoformat()),
            )

    def load_risk_state(self) -> str | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM risk_control_state WHERE id = 1"
            ).fetchone()
        return row["payload"] if row else None

    def save_order_record(self, exchange_id: str, record) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO order_ledger
                    (exchange_id, order_id, client_order_id, plan_id, symbol,
                     status, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange_id, order_id) DO UPDATE SET
                    client_order_id = excluded.client_order_id,
                    plan_id = excluded.plan_id,
                    symbol = excluded.symbol,
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    exchange_id,
                    record.order.id,
                    record.order.client_order_id,
                    record.order.plan_id,
                    record.order.symbol,
                    str(record.order.status),
                    record.model_dump_json(),
                    record.last_reconciled_at.isoformat(),
                ),
            )

    def load_order_records(self, exchange_id: str) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM order_ledger
                WHERE exchange_id = ? ORDER BY updated_at DESC
                """,
                (exchange_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_multi_leg_execution_run(self, run) -> None:
        now = run.updated_at.isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_fingerprint FROM multi_leg_execution_runs
                WHERE run_id = ?
                """,
                (run.id,),
            ).fetchone()
            if existing and existing["request_fingerprint"] != run.request_fingerprint:
                connection.rollback()
                raise RuntimeError("multi-leg run id is already bound to another request")
            connection.execute(
                """
                INSERT INTO multi_leg_execution_runs
                    (run_id, request_fingerprint, status, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    run.id,
                    run.request_fingerprint,
                    str(run.status),
                    run.model_dump_json(),
                    run.created_at.isoformat(),
                    now,
                ),
            )
            connection.commit()

    def load_multi_leg_execution_run(self, run_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM multi_leg_execution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_multi_leg_execution_runs(self, limit: int = 100) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM multi_leg_execution_runs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (min(max(limit, 1), 10000),),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_multi_leg_commissioning(self, result) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO multi_leg_commissioning
                    (exchange_id, strategy_kind, credential_fingerprint,
                     fingerprint, passed, payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange_id, strategy_kind) DO UPDATE SET
                    credential_fingerprint = excluded.credential_fingerprint,
                    fingerprint = excluded.fingerprint,
                    passed = excluded.passed,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    result.exchange,
                    result.strategy_kind,
                    result.credential_fingerprint,
                    result.fingerprint,
                    int(result.passed),
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def load_multi_leg_commissioning(
        self,
        exchange_id: str,
        strategy_kind: str,
    ) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM multi_leg_commissioning
                WHERE exchange_id = ? AND strategy_kind = ?
                """,
                (exchange_id, strategy_kind),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_multi_leg_commissioning(self) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM multi_leg_commissioning
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def daily_equity_baseline(self, account_id: str, current_equity) -> str:
        today = datetime.now(UTC).date().isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT session_date, equity FROM account_daily_baseline
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            if row and row["session_date"] == today:
                connection.commit()
                return str(row["equity"])
            value = str(current_equity)
            connection.execute(
                """
                INSERT INTO account_daily_baseline
                    (account_id, session_date, equity, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_date = excluded.session_date,
                    equity = excluded.equity,
                    updated_at = excluded.updated_at
                """,
                (account_id, today, value, datetime.now(UTC).isoformat()),
            )
            connection.commit()
        return value

    def append_audit(self, event) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO audit_events
                    (event_id, category, severity, message, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.category,
                    event.severity,
                    event.message,
                    event.model_dump_json(),
                    event.timestamp.isoformat(),
                ),
            )

    def load_audit(self, limit: int = 100) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM audit_events ORDER BY sequence DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_candles(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: str,
        candles,
    ) -> None:
        received_at = datetime.now(UTC).isoformat()
        rows = [
            (
                exchange_id,
                symbol,
                timeframe,
                candle.timestamp.isoformat(),
                str(candle.open),
                str(candle.high),
                str(candle.low),
                str(candle.close),
                str(candle.volume),
                received_at,
            )
            for candle in candles
        ]
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT INTO market_candles
                    (exchange_id, symbol, timeframe, timestamp, open, high, low,
                     close, volume, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange_id, symbol, timeframe, timestamp) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    received_at = excluded.received_at
                """,
                rows,
            )
            connection.commit()

    def load_candles(
        self,
        exchange_id: str,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> list[dict]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, open, high, low, close, volume, received_at
                FROM market_candles
                WHERE exchange_id = ? AND symbol = ? AND timeframe = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (exchange_id, symbol, timeframe, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_paper_state(self, account_id: str, payload: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO paper_account_state (account_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (account_id, payload, datetime.now(UTC).isoformat()),
            )
            connection.commit()

    def load_paper_state(self, account_id: str) -> str | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM paper_account_state WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return row["payload"] if row else None
