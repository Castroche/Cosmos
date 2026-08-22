from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
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
        self._backup_before_schema_upgrade(7)
        self._initialize()

    def _backup_before_schema_upgrade(self, target_version: int) -> Path | None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        source = sqlite3.connect(self.path, timeout=30)
        try:
            has_migrations = source.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            current_version = (
                int(
                    source.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                )
                if has_migrations
                else 0
            )
            if current_version >= target_version:
                return None
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            destination = (
                self.path.parent
                / "backups"
                / (f"{self.path.stem}-pre-schema-v{target_version}-{stamp}.db")
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
                target.commit()
                check = target.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("pre-migration database backup failed quick_check")
            finally:
                target.close()
            return destination
        finally:
            source.close()

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
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                );
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                    event_time TEXT,
                    available_time TEXT,
                    source_time TEXT,
                    ingestion_time TEXT,
                    revision_policy TEXT,
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
                CREATE TABLE IF NOT EXISTS research_hypotheses_v2 (
                    hypothesis_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    parent_hypothesis_id TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_hypotheses_family
                    ON research_hypotheses_v2(family_id, created_at);
                CREATE TABLE IF NOT EXISTS research_trials (
                    trial_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    research_event_id TEXT NOT NULL,
                    campaign_id TEXT,
                    hypothesis_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    parent_trial_id TEXT,
                    trial_type TEXT NOT NULL,
                    touched_returns INTEGER NOT NULL,
                    touched_blind INTEGER NOT NULL,
                    touched_shadow INTEGER NOT NULL,
                    dataset_fingerprint TEXT NOT NULL,
                    validation_fingerprint TEXT NOT NULL,
                    artifact_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_research_trials_family_created
                    ON research_trials(family_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_research_trials_event
                    ON research_trials(research_event_id, created_at);
                CREATE TABLE IF NOT EXISTS research_trial_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (trial_id) REFERENCES research_trials(trial_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_research_trial_events_trial
                    ON research_trial_events(trial_id, sequence);
                CREATE TABLE IF NOT EXISTS online_fdr_streams (
                    stream_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS online_fdr_events (
                    stream_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    alpha TEXT NOT NULL,
                    p_value TEXT,
                    rejected INTEGER,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (stream_id, sequence),
                    FOREIGN KEY (stream_id) REFERENCES online_fdr_streams(stream_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS autonomous_research_events (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    snapshot_ready_at TEXT NOT NULL,
                    decision_committed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_autonomous_research_events_updated
                    ON autonomous_research_events(updated_at DESC);
                CREATE TABLE IF NOT EXISTS autonomous_validation_runs (
                    validation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES autonomous_research_events(run_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS autonomous_research_budgets (
                    research_event_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    semantic_proposals INTEGER NOT NULL DEFAULT 0,
                    empirical_trials INTEGER NOT NULL DEFAULT 0,
                    exact_finalists INTEGER NOT NULL DEFAULT 0,
                    evolution_rounds INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (research_event_id, family_id)
                );
                CREATE TABLE IF NOT EXISTS autonomous_research_budget_events (
                    idempotency_key TEXT PRIMARY KEY,
                    research_event_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    budget_kind TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_artifact_index (
                    artifact_fingerprint TEXT PRIMARY KEY,
                    artifact_kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_behavior_fingerprints (
                    behavior_fingerprint TEXT PRIMARY KEY,
                    ir_hash TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_novelty_index (
                    novelty_kind TEXT NOT NULL,
                    novelty_fingerprint TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (novelty_kind, novelty_fingerprint, family_id)
                );
                CREATE TABLE IF NOT EXISTS research_candidate_lineage_v2 (
                    lineage_id TEXT PRIMARY KEY,
                    research_event_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    parent_ids TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generator_envelopes (
                    envelope_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_certificates_v2 (
                    certificate_id TEXT PRIMARY KEY,
                    certificate_type TEXT NOT NULL,
                    strategy_ir_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS certificate_revocations (
                    revocation_id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_certificate_revocations_certificate
                    ON certificate_revocations(certificate_id, created_at);
                CREATE TABLE IF NOT EXISTS shadow_evidence_v2 (
                    evidence_id TEXT PRIMARY KEY,
                    strategy_ir_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
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
                CREATE TABLE IF NOT EXISTS personal_strategy_library (
                    item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    family TEXT NOT NULL,
                    status TEXT NOT NULL,
                    spec_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_personal_strategy_library_status
                    ON personal_strategy_library(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS strategy_library_preferences (
                    item_id TEXT PRIMARY KEY,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS agent_launch_profiles (
                    profile_id TEXT PRIMARY KEY,
                    autonomy_mode TEXT NOT NULL,
                    grant_fingerprint TEXT NOT NULL,
                    authorized_fingerprint TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_launch_profiles_updated
                    ON agent_launch_profiles(updated_at DESC);
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
            temporal_tables = (
                "research_dataset_candles",
                "research_dataset_funding",
                "research_dataset_open_interest",
                "research_dataset_positioning",
                "research_dataset_basis",
                "research_dataset_order_flow",
                "research_dataset_liquidation_flow",
            )
            for table in temporal_tables:
                temporal_columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column in (
                    "event_time",
                    "available_time",
                    "source_time",
                    "ingestion_time",
                    "revision_policy",
                ):
                    if column not in temporal_columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_candidates_champion
                ON strategy_candidates(family) WHERE status = 'champion'
                """
            )
            now = datetime.now(UTC).isoformat()
            connection.executemany(
                """
                INSERT OR IGNORE INTO schema_migrations (version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                [
                    (1, "legacy_schema_baseline", now),
                    (2, "autonomous_research_integrity", now),
                    (3, "point_in_time_fact_metadata", now),
                    (4, "autonomous_research_v2_artifacts", now),
                    (5, "autonomous_research_persistent_budgets", now),
                    (6, "autonomous_research_novelty_index", now),
                    (7, "operator_workspace_and_personal_strategy_library", now),
                ],
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

        def temporal_values(point, *, closed_bar: bool = False) -> tuple[str, str, str, str, str]:
            timestamp = point.timestamp
            derived_available = timestamp
            if closed_bar and getattr(dataset, "timeframe_seconds", 0) > 0:
                from datetime import timedelta

                derived_available = timestamp + timedelta(seconds=dataset.timeframe_seconds)
            event_time = getattr(point, "event_time", None) or timestamp
            available_time = getattr(point, "available_time", None) or derived_available
            source_time = getattr(point, "source_time", None) or available_time
            ingestion_time = getattr(point, "ingestion_time", None) or max(
                dataset.created_at,
                available_time,
            )
            revision_policy = getattr(point, "revision_policy", None) or (
                "append_only_closed_bar" if closed_bar else "exchange_published_append_only"
            )
            return (
                event_time.isoformat(),
                available_time.isoformat(),
                source_time.isoformat(),
                ingestion_time.isoformat(),
                str(revision_policy),
            )

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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy,
                     open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        candle.timestamp.isoformat(),
                        *temporal_values(candle, closed_bar=True),
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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy, rate, mark_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point),
                        str(point.rate),
                        str(point.mark_price) if point.mark_price is not None else None,
                    )
                    for index, point in enumerate(funding_rates or [])
                ],
            )
            connection.executemany(
                """
                INSERT INTO research_dataset_open_interest
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy,
                     open_interest_amount, open_interest_value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point),
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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy, long_short_ratio,
                     long_account_fraction, short_account_fraction, source_variant)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point),
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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy,
                     mark_price, index_price, basis_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point),
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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy, aggregate_count,
                     underlying_trade_count, base_volume, quote_volume,
                     taker_buy_base_volume, taker_sell_base_volume,
                     delta_base_volume, maximum_trade_quote)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point, closed_bar=True),
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
                    (dataset_id, sequence, timestamp, event_time, available_time,
                     source_time, ingestion_time, revision_policy, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset.id,
                        index,
                        point.timestamp.isoformat(),
                        *temporal_values(point),
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy, open, high, low, close, volume
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy, rate, mark_price
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy,
                       open_interest_amount, open_interest_value
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy,
                       long_short_ratio, long_account_fraction,
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy, mark_price, index_price, basis_rate
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
                SELECT timestamp, event_time, available_time, source_time,
                       ingestion_time, revision_policy,
                       aggregate_count, underlying_trade_count,
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
                SELECT payload, event_time, available_time, source_time,
                       ingestion_time, revision_policy
                FROM research_dataset_liquidation_flow
                WHERE dataset_id = ? ORDER BY sequence
                """,
                (dataset_id,),
            ).fetchall()
        result: list[dict] = []
        for row in rows:
            payload = json.loads(row["payload"])
            for field in (
                "event_time",
                "available_time",
                "source_time",
                "ingestion_time",
                "revision_policy",
            ):
                if row[field] is not None:
                    payload[field] = row[field]
            result.append(payload)
        return result

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
                raise RuntimeError("catalog journal append would fork or skip the durable chain")
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
            effective_trial_count = cumulative_hypothesis_count + sequential_experiment_count - 1
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

    def save_personal_strategy_library_item(self, item) -> None:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        now = str(payload["updated_at"])
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO personal_strategy_library
                    (item_id, name, family, status, spec_fingerprint, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    name = excluded.name,
                    family = excluded.family,
                    status = excluded.status,
                    spec_fingerprint = excluded.spec_fingerprint,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    payload["id"],
                    payload["name"],
                    payload["family"],
                    payload["status"],
                    payload["spec_fingerprint"],
                    json.dumps(payload, ensure_ascii=False, default=str),
                    str(payload["created_at"]),
                    now,
                ),
            )

    def load_personal_strategy_library_item(self, item_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM personal_strategy_library WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_personal_strategy_library_items(self, limit: int = 500) -> list[dict]:
        bounded = min(max(limit, 1), 1000)
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM personal_strategy_library
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def delete_personal_strategy_library_item(self, item_id: str) -> bool:
        with self._lock, self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM personal_strategy_library WHERE item_id = ?",
                (item_id,),
            ).rowcount
        return bool(deleted)

    def set_strategy_library_hidden(self, item_id: str, hidden: bool) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_library_preferences (item_id, hidden, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    hidden = excluded.hidden,
                    updated_at = excluded.updated_at
                """,
                (item_id, int(hidden), datetime.now(UTC).isoformat()),
            )

    def hidden_strategy_library_items(self) -> set[str]:
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT item_id FROM strategy_library_preferences WHERE hidden = 1"
            ).fetchall()
        return {str(row["item_id"]) for row in rows}

    def save_agent_launch_profile(self, profile) -> None:
        payload = (
            profile.model_dump(mode="json")
            if hasattr(profile, "model_dump")
            else dict(profile)
        )
        with self._lock, self.connect() as connection:
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
                    payload["id"],
                    payload["autonomy_mode"],
                    payload["grant_fingerprint"],
                    payload.get("authorized_fingerprint"),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    str(payload["created_at"]),
                    str(payload["updated_at"]),
                ),
            )

    def load_agent_launch_profile(self, profile_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_launch_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_agent_launch_profiles(self, limit: int = 100) -> list[dict]:
        bounded = min(max(limit, 1), 200)
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM agent_launch_profiles
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
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

    def page_audit(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        category: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int | None]:
        bounded = min(max(limit, 1), 200)
        clauses: list[str] = []
        parameters: list[object] = []
        if before_sequence is not None:
            clauses.append("sequence < ?")
            parameters.append(before_sequence)
        if category:
            clauses.append("category = ?")
            parameters.append(category)
        if severity:
            clauses.append("severity = ?")
            parameters.append(severity)
        if search:
            clauses.append("(message LIKE ? OR payload LIKE ?)")
            needle = f"%{search}%"
            parameters.extend([needle, needle])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(bounded + 1)
        with self._lock, self.connect() as connection:
            rows = connection.execute(
                "SELECT sequence, payload FROM audit_events"
                + where
                + " ORDER BY sequence DESC LIMIT ?",
                parameters,
            ).fetchall()
        has_more = len(rows) > bounded
        selected = rows[:bounded]
        next_cursor = int(selected[-1]["sequence"]) if has_more and selected else None
        return [json.loads(row["payload"]) for row in selected], next_cursor

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

    def reserve_research_trial(self, trial) -> dict:
        """Persist an immutable trial before any return-bearing evidence is read."""

        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT trial_id FROM research_trials WHERE idempotency_key = ?",
                (trial.idempotency_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO research_trials
                        (trial_id, idempotency_key, research_event_id, campaign_id,
                         hypothesis_id, family_id, candidate_id, parent_trial_id,
                         trial_type, touched_returns, touched_blind, touched_shadow,
                         dataset_fingerprint, validation_fingerprint,
                         artifact_fingerprint, payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trial.id,
                        trial.idempotency_key,
                        trial.research_event_id,
                        trial.campaign_id,
                        trial.hypothesis_id,
                        trial.family_id,
                        trial.candidate_id,
                        trial.parent_trial_id,
                        str(trial.trial_type),
                        int(trial.touched_returns),
                        int(trial.touched_blind),
                        int(trial.touched_shadow),
                        trial.dataset_fingerprint,
                        trial.validation_fingerprint,
                        trial.artifact_fingerprint,
                        trial.model_dump_json(),
                        trial.created_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO research_trial_events
                        (trial_id, event_type, payload, created_at)
                    VALUES (?, 'reserved', ?, ?)
                    """,
                    (trial.id, trial.model_dump_json(), now),
                )
                trial_id = trial.id
            else:
                trial_id = existing["trial_id"]
            connection.commit()
        loaded = self.load_research_trial(trial_id)
        if loaded is None:
            raise RuntimeError("reserved research trial could not be reloaded")
        return loaded

    def append_research_trial_result(
        self,
        trial_id: str,
        *,
        result: str,
        p_value: Decimal | None = None,
        adjusted_p_value: Decimal | None = None,
        dsr: Decimal | None = None,
        pbo: Decimal | None = None,
        global_alpha: Decimal | None = None,
        family_alpha: Decimal | None = None,
        reason_codes: list[str] | None = None,
    ) -> dict:
        payload = {
            "result": result,
            "p_value": str(p_value) if p_value is not None else None,
            "adjusted_p_value": (str(adjusted_p_value) if adjusted_p_value is not None else None),
            "dsr": str(dsr) if dsr is not None else None,
            "pbo": str(pbo) if pbo is not None else None,
            "global_alpha": str(global_alpha) if global_alpha is not None else None,
            "family_alpha": str(family_alpha) if family_alpha is not None else None,
            "reason_codes": list(reason_codes or []),
        }
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM research_trials WHERE trial_id = ?", (trial_id,)
                ).fetchone()
                is None
            ):
                raise LookupError(f"research trial not found: {trial_id}")
            connection.execute(
                """
                INSERT INTO research_trial_events
                    (trial_id, event_type, payload, created_at)
                VALUES (?, 'result', ?, ?)
                """,
                (trial_id, json.dumps(payload, sort_keys=True), now),
            )
            connection.commit()
        loaded = self.load_research_trial(trial_id)
        if loaded is None:
            raise RuntimeError("completed research trial could not be reloaded")
        return loaded

    def load_research_trial(self, trial_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_trials WHERE trial_id = ?", (trial_id,)
            ).fetchone()
            if row is None:
                return None
            events = connection.execute(
                """
                SELECT event_type, payload FROM research_trial_events
                WHERE trial_id = ? ORDER BY sequence
                """,
                (trial_id,),
            ).fetchall()
        payload = json.loads(row["payload"])
        for event in events:
            if event["event_type"] == "result":
                update = json.loads(event["payload"])
                payload.update({key: value for key, value in update.items() if value is not None})
        return payload

    def list_research_trials(
        self,
        *,
        family_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        bounded = min(max(limit, 1), 10000)
        with self._lock, self.connect() as connection:
            if family_id:
                rows = connection.execute(
                    """
                    SELECT trial_id FROM research_trials WHERE family_id = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (family_id, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT trial_id FROM research_trials
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (bounded,),
                ).fetchall()
        return [
            loaded
            for row in rows
            if (loaded := self.load_research_trial(row["trial_id"])) is not None
        ]

    def allocate_online_fdr(
        self,
        *,
        stream_id: str,
        scope: str,
        target_fdr: Decimal,
        idempotency_key: str,
    ) -> dict:
        from .research_integrity import OnlineFDRSnapshot, lord_plus_plus_alpha

        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_event = connection.execute(
                "SELECT payload FROM online_fdr_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_event is not None:
                connection.commit()
                return json.loads(existing_event["payload"])["allocation"]
            row = connection.execute(
                "SELECT scope, payload FROM online_fdr_streams WHERE stream_id = ?",
                (stream_id,),
            ).fetchone()
            if row is None:
                snapshot = OnlineFDRSnapshot(
                    stream_id=stream_id,
                    scope=scope,
                    target_fdr=target_fdr,
                    initial_wealth=target_fdr / Decimal("2"),
                    next_sequence=1,
                )
                connection.execute(
                    """
                    INSERT INTO online_fdr_streams (stream_id, scope, payload, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (stream_id, scope, snapshot.model_dump_json(), now),
                )
            else:
                if row["scope"] != scope:
                    raise RuntimeError("online FDR stream scope cannot change")
                try:
                    snapshot = OnlineFDRSnapshot.model_validate_json(row["payload"])
                except Exception:
                    snapshot = OnlineFDRSnapshot(
                        stream_id=stream_id,
                        scope=scope,
                        target_fdr=target_fdr,
                        initial_wealth=target_fdr / Decimal("2"),
                        next_sequence=1,
                        method="non_replenishing_alpha_spending_v1",
                        degraded=True,
                    )
            sequence = snapshot.next_sequence
            alpha = lord_plus_plus_alpha(snapshot, sequence)
            allocation = {
                "stream_id": stream_id,
                "sequence": sequence,
                "alpha": str(alpha),
                "method": snapshot.method,
                "idempotency_key": idempotency_key,
            }
            snapshot = snapshot.model_copy(
                update={
                    "next_sequence": sequence + 1,
                    "pending_sequences": [*snapshot.pending_sequences, sequence],
                }
            )
            event_payload = {"allocation": allocation, "status": "pending"}
            connection.execute(
                """
                INSERT INTO online_fdr_events
                    (stream_id, sequence, idempotency_key, alpha, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    stream_id,
                    sequence,
                    idempotency_key,
                    str(alpha),
                    json.dumps(event_payload, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                "UPDATE online_fdr_streams SET payload = ?, updated_at = ? WHERE stream_id = ?",
                (snapshot.model_dump_json(), now, stream_id),
            )
            connection.commit()
        return allocation

    def record_online_fdr_result(
        self,
        *,
        stream_id: str,
        sequence: int,
        idempotency_key: str,
        p_value: Decimal,
    ) -> dict:
        from .research_integrity import OnlineFDRSnapshot

        if not Decimal("0") <= p_value <= Decimal("1"):
            raise ValueError("online FDR p-value must be between zero and one")
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            stream = connection.execute(
                "SELECT payload FROM online_fdr_streams WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            event = connection.execute(
                """
                SELECT alpha, p_value, payload FROM online_fdr_events
                WHERE stream_id = ? AND sequence = ? AND idempotency_key = ?
                """,
                (stream_id, sequence, idempotency_key),
            ).fetchone()
            if stream is None or event is None:
                raise LookupError("online FDR allocation not found")
            snapshot = OnlineFDRSnapshot.model_validate_json(stream["payload"])
            if event["p_value"] is not None:
                if Decimal(event["p_value"]) != p_value:
                    raise RuntimeError("online FDR result is immutable")
                connection.commit()
                return snapshot.model_dump(mode="json")
            alpha = Decimal(event["alpha"])
            rejected = p_value <= alpha
            pending = [item for item in snapshot.pending_sequences if item != sequence]
            rejections = list(snapshot.rejection_sequences)
            if rejected and sequence not in rejections:
                rejections.append(sequence)
                rejections.sort()
            snapshot = snapshot.model_copy(
                update={
                    "pending_sequences": pending,
                    "rejection_sequences": rejections,
                }
            )
            event_payload = json.loads(event["payload"])
            event_payload.update(
                {"status": "completed", "p_value": str(p_value), "rejected": rejected}
            )
            connection.execute(
                """
                UPDATE online_fdr_events
                SET p_value = ?, rejected = ?, payload = ?, completed_at = ?
                WHERE stream_id = ? AND sequence = ?
                """,
                (
                    str(p_value),
                    int(rejected),
                    json.dumps(event_payload, sort_keys=True),
                    now,
                    stream_id,
                    sequence,
                ),
            )
            connection.execute(
                "UPDATE online_fdr_streams SET payload = ?, updated_at = ? WHERE stream_id = ?",
                (snapshot.model_dump_json(), now, stream_id),
            )
            connection.commit()
        return snapshot.model_dump(mode="json")

    def load_online_fdr_snapshot(self, stream_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM online_fdr_streams WHERE stream_id = ?", (stream_id,)
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def restore_online_fdr_snapshot(self, payload: dict) -> None:
        from .research_integrity import OnlineFDRSnapshot

        try:
            snapshot = OnlineFDRSnapshot.model_validate(payload)
        except Exception as exc:
            raise RuntimeError(
                "online FDR snapshot restore cannot roll back or accept inconsistent state"
            ) from exc
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM online_fdr_streams WHERE stream_id = ?",
                (snapshot.stream_id,),
            ).fetchone()
            if existing is not None:
                current = OnlineFDRSnapshot.model_validate_json(existing["payload"])
                if snapshot.next_sequence < current.next_sequence or not set(
                    current.rejection_sequences
                ).issubset(snapshot.rejection_sequences):
                    raise RuntimeError("online FDR snapshot restore cannot roll back spent wealth")
            connection.execute(
                """
                INSERT INTO online_fdr_streams (stream_id, scope, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(stream_id) DO UPDATE SET
                    scope = excluded.scope,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot.stream_id,
                    snapshot.scope,
                    snapshot.model_dump_json(),
                    now,
                ),
            )
            connection.commit()

    def save_research_hypothesis_v2(self, hypothesis, *, parent_id: str | None = None) -> dict:
        now = datetime.now(UTC).isoformat()
        payload = hypothesis.model_dump_json()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM research_hypotheses_v2 WHERE hypothesis_id = ?",
                (hypothesis.id,),
            ).fetchone()
            if existing is not None and existing["payload"] != payload:
                raise RuntimeError("research hypothesis is immutable")
            connection.execute(
                """
                INSERT OR IGNORE INTO research_hypotheses_v2
                    (hypothesis_id, family_id, parent_hypothesis_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (hypothesis.id, hypothesis.family_id, parent_id, payload, now),
            )
            connection.commit()
        return json.loads(payload)

    def create_autonomous_research_event(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        snapshot_ready_at: datetime,
        payload: dict,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM autonomous_research_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO autonomous_research_events
                        (run_id, idempotency_key, status, payload, snapshot_ready_at,
                         created_at, updated_at)
                    VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        idempotency_key,
                        encoded,
                        snapshot_ready_at.isoformat(),
                        now,
                        now,
                    ),
                )
            connection.commit()
        return json.loads(existing["payload"]) if existing is not None else payload

    def consume_autonomous_research_budget(
        self,
        *,
        research_event_id: str,
        family_id: str,
        budget_kind: str,
        amount: int,
        limit: int,
        idempotency_key: str,
    ) -> bool:
        columns = {
            "semantic_proposals": "semantic_proposals",
            "empirical_trials": "empirical_trials",
            "exact_finalists": "exact_finalists",
            "evolution_rounds": "evolution_rounds",
        }
        if budget_kind not in columns or amount <= 0 or limit <= 0:
            raise ValueError("invalid autonomous research budget request")
        column = columns[budget_kind]
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    """
                SELECT 1 FROM autonomous_research_budget_events
                WHERE idempotency_key = ?
                """,
                    (idempotency_key,),
                ).fetchone()
                is not None
            ):
                connection.commit()
                return True
            row = connection.execute(
                """
                SELECT semantic_proposals, empirical_trials, exact_finalists,
                       evolution_rounds
                FROM autonomous_research_budgets
                WHERE research_event_id = ? AND family_id = ?
                """,
                (research_event_id, family_id),
            ).fetchone()
            current = int(row[column]) if row else 0
            if current + amount > limit:
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO autonomous_research_budgets
                    (research_event_id, family_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(research_event_id, family_id) DO NOTHING
                """,
                (research_event_id, family_id, now),
            )
            connection.execute(
                f"""
                UPDATE autonomous_research_budgets
                SET {column} = {column} + ?, updated_at = ?
                WHERE research_event_id = ? AND family_id = ?
                """,
                (amount, now, research_event_id, family_id),
            )
            connection.execute(
                """
                INSERT INTO autonomous_research_budget_events
                    (idempotency_key, research_event_id, family_id, budget_kind,
                     amount, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    research_event_id,
                    family_id,
                    budget_kind,
                    amount,
                    now,
                ),
            )
            connection.commit()
        return True

    def commit_autonomous_research_decision(
        self,
        result,
        *,
        trial_result: dict | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        encoded = result.model_dump_json()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, payload FROM autonomous_research_events WHERE run_id = ?",
                (result.run_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"autonomous research run not found: {result.run_id}")
            if row["status"] == "COMMITTED":
                connection.commit()
                return json.loads(row["payload"])
            if trial_result is not None:
                trial_id = result.trial_id
                if (
                    not trial_id
                    or connection.execute(
                        "SELECT 1 FROM research_trials WHERE trial_id = ?", (trial_id,)
                    ).fetchone()
                    is None
                ):
                    raise RuntimeError("atomic research decision references no reserved trial")
                connection.execute(
                    """
                    INSERT INTO research_trial_events
                        (trial_id, event_type, payload, created_at)
                    VALUES (?, 'result', ?, ?)
                    """,
                    (trial_id, json.dumps(trial_result, sort_keys=True), now),
                )
            connection.execute(
                """
                UPDATE autonomous_research_events
                SET status = 'COMMITTED', payload = ?, decision_committed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (encoded, now, now, result.run_id),
            )
            connection.commit()
        return json.loads(encoded)

    def load_autonomous_research_event(self, run_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM autonomous_research_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def put_research_artifact(
        self,
        *,
        artifact_fingerprint: str,
        artifact_kind: str,
        payload: dict,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT artifact_kind, payload FROM research_artifact_index
                WHERE artifact_fingerprint = ?
                """,
                (artifact_fingerprint,),
            ).fetchone()
            if existing is not None and (
                existing["artifact_kind"] != artifact_kind or existing["payload"] != encoded
            ):
                raise RuntimeError("content-addressed research artifact is immutable")
            connection.execute(
                """
                INSERT INTO research_artifact_index
                    (artifact_fingerprint, artifact_kind, payload, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(artifact_fingerprint) DO UPDATE SET last_used_at = excluded.last_used_at
                """,
                (artifact_fingerprint, artifact_kind, encoded, now, now),
            )
            connection.commit()

    def load_research_artifact(self, artifact_fingerprint: str, artifact_kind: str) -> dict | None:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload FROM research_artifact_index
                WHERE artifact_fingerprint = ? AND artifact_kind = ?
                """,
                (artifact_fingerprint, artifact_kind),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE research_artifact_index SET last_used_at = ?
                    WHERE artifact_fingerprint = ?
                    """,
                    (now, artifact_fingerprint),
                )
            connection.commit()
        return json.loads(row["payload"]) if row else None

    def register_behavior_fingerprint(
        self,
        *,
        behavior_fingerprint: str,
        ir_hash: str,
        family_id: str,
        payload: dict,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO research_behavior_fingerprints
                    (behavior_fingerprint, ir_hash, family_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    behavior_fingerprint,
                    ir_hash,
                    family_id,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            created = cursor.rowcount == 1
            connection.commit()
        return created

    def register_research_novelty(
        self,
        *,
        novelty_kind: str,
        novelty_fingerprint: str,
        family_id: str,
        payload: dict,
    ) -> bool:
        if novelty_kind not in {"ast", "feature_graph"}:
            raise ValueError("unsupported novelty fingerprint kind")
        now = datetime.now(UTC).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO research_novelty_index
                    (novelty_kind, novelty_fingerprint, family_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    novelty_kind,
                    novelty_fingerprint,
                    family_id,
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            created = cursor.rowcount == 1
            connection.commit()
        return created

    def save_candidate_lineage_v2(
        self,
        *,
        lineage_id: str,
        research_event_id: str,
        family_id: str,
        candidate_id: str,
        parent_ids: list[str],
        operation: str,
        payload: dict,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, sort_keys=True)
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM research_candidate_lineage_v2 WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
            if existing is not None and existing["payload"] != encoded:
                raise RuntimeError("candidate lineage is immutable")
            connection.execute(
                """
                INSERT OR IGNORE INTO research_candidate_lineage_v2
                    (lineage_id, research_event_id, family_id, candidate_id,
                     parent_ids, operation, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lineage_id,
                    research_event_id,
                    family_id,
                    candidate_id,
                    json.dumps(parent_ids),
                    operation,
                    encoded,
                    now,
                ),
            )
            connection.commit()

    def save_generator_envelope(self, envelope) -> dict:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO generator_envelopes
                    (envelope_id, fingerprint, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    envelope.id,
                    envelope.fingerprint,
                    envelope.model_dump_json(),
                    envelope.issued_at.isoformat(),
                ),
            )
            connection.commit()
        return envelope.model_dump(mode="json")

    def load_generator_envelope(self, envelope_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM generator_envelopes WHERE envelope_id = ?",
                (envelope_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_research_certificate(self, certificate, certificate_type: str) -> dict:
        strategy_ir_hash = getattr(certificate, "strategy_ir_hash", "")
        created_at = getattr(certificate, "issued_at", None) or getattr(
            certificate, "created_at", datetime.now(UTC)
        )
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO research_certificates_v2
                    (certificate_id, certificate_type, strategy_ir_hash, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    certificate.id,
                    certificate_type,
                    strategy_ir_hash,
                    certificate.model_dump_json(),
                    created_at.isoformat(),
                ),
            )
            connection.commit()
        return certificate.model_dump(mode="json")

    def revoke_research_certificate(self, event) -> dict:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM research_certificates_v2 WHERE certificate_id = ?",
                    (event.certificate_id,),
                ).fetchone()
                is None
            ):
                raise LookupError(f"research certificate not found: {event.certificate_id}")
            connection.execute(
                """
                INSERT OR IGNORE INTO certificate_revocations
                    (revocation_id, certificate_id, reason_code, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.certificate_id,
                    event.reason_code,
                    event.model_dump_json(),
                    event.revoked_at.isoformat(),
                ),
            )
            connection.commit()
        return event.model_dump(mode="json")

    def load_research_certificate_status(self, certificate_id: str) -> dict | None:
        with self._lock, self.connect() as connection:
            certificate = connection.execute(
                """
                SELECT certificate_type, payload FROM research_certificates_v2
                WHERE certificate_id = ?
                """,
                (certificate_id,),
            ).fetchone()
            if certificate is None:
                return None
            revocations = connection.execute(
                """
                SELECT payload FROM certificate_revocations
                WHERE certificate_id = ? ORDER BY created_at
                """,
                (certificate_id,),
            ).fetchall()
        return {
            "certificate_type": certificate["certificate_type"],
            "certificate": json.loads(certificate["payload"]),
            "revoked": bool(revocations),
            "revocations": [json.loads(row["payload"]) for row in revocations],
        }
