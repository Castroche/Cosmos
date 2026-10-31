from __future__ import annotations

import json
from datetime import UTC, datetime


class MigrationsRepository:
    def __init__(self, store):
        self.store = store

    def _initialize(self) -> None:
        with self.store._lock, self.store.connect() as connection:
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
                CREATE TABLE IF NOT EXISTS strategy_review_jobs (
                    job_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    review_fingerprint TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_strategy_review_jobs_status
                    ON strategy_review_jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_strategy_review_jobs_candidate
                    ON strategy_review_jobs(candidate_id, updated_at DESC);
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
                CREATE TABLE IF NOT EXISTS alpha_model_artifacts_v1 (
                    model_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alpha_models_profile_created
                    ON alpha_model_artifacts_v1(profile, created_at DESC);
                CREATE TABLE IF NOT EXISTS sandbox_admission_handoffs_v1 (
                    certificate_id TEXT PRIMARY KEY,
                    exchange_id TEXT NOT NULL,
                    trading_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (certificate_id) REFERENCES research_certificates_v2(certificate_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS sandbox_execution_evidence_v1 (
                    evidence_id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL UNIQUE,
                    aligned INTEGER NOT NULL CHECK (aligned IN (0, 1)),
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (certificate_id) REFERENCES research_certificates_v2(certificate_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_sandbox_execution_certificate
                    ON sandbox_execution_evidence_v1(certificate_id, created_at);
                CREATE TABLE IF NOT EXISTS live_strategy_selections_v2 (
                    certificate_id TEXT PRIMARY KEY,
                    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
                    certificate_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (certificate_id) REFERENCES research_certificates_v2(certificate_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS persistent_live_authorization_v1 (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    grant_id TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    scope_fingerprint TEXT NOT NULL,
                    configuration_fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_authorization_events_v1 (
                    event_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_live_authorization_events_created
                    ON live_authorization_events_v1(created_at DESC);
                CREATE TABLE IF NOT EXISTS ti_market_states_v2 (
                    state_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    state_hash TEXT NOT NULL UNIQUE,
                    data_as_of TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_market_states_symbol_asof
                    ON ti_market_states_v2(symbol, data_as_of DESC);
                CREATE TABLE IF NOT EXISTS ti_theses_v2 (
                    thesis_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_theses_symbol_state
                    ON ti_theses_v2(symbol, state, updated_at DESC);
                CREATE TABLE IF NOT EXISTS ti_plans_v2 (
                    plan_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (thesis_id) REFERENCES ti_theses_v2(thesis_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_ti_plans_symbol_state
                    ON ti_plans_v2(symbol, state, updated_at DESC);
                CREATE TABLE IF NOT EXISTS ti_quant_predictions_v1 (
                    prediction_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    model_fingerprint TEXT NOT NULL,
                    data_as_of TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_quant_symbol_asof
                    ON ti_quant_predictions_v1(symbol, data_as_of DESC);
                CREATE TABLE IF NOT EXISTS ti_decision_episodes_v2 (
                    episode_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    market_state_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_episodes_symbol_updated
                    ON ti_decision_episodes_v2(symbol, updated_at DESC);
                CREATE TABLE IF NOT EXISTS ti_artifacts_v2 (
                    artifact_id TEXT PRIMARY KEY,
                    artifact_type TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_artifacts_type_created
                    ON ti_artifacts_v2(artifact_type, created_at DESC);
                CREATE TABLE IF NOT EXISTS ti_events_v2 (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    aggregate_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_events_aggregate
                    ON ti_events_v2(aggregate_type, aggregate_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_ti_events_queue
                    ON ti_events_v2(event_type, aggregate_id, sequence);
                CREATE TABLE IF NOT EXISTS ti_decision_queue_cursors_v2 (
                    symbol TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ti_wake_firings_v2 (
                    condition_id TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    PRIMARY KEY (condition_id, event_key),
                    UNIQUE (condition_id),
                    FOREIGN KEY (event_id) REFERENCES ti_events_v2(event_id)
                        ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS ti_live_grants_v2 (
                    grant_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    configuration_fingerprint TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_live_grants_status_expiry
                    ON ti_live_grants_v2(status, expires_at DESC);
                CREATE TABLE IF NOT EXISTS ti_entry_orders_v3 (
                    entry_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ti_entry_orders_scope
                    ON ti_entry_orders_v3(mode, symbol, side, status);
                CREATE INDEX IF NOT EXISTS idx_ti_entry_orders_recent
                    ON ti_entry_orders_v3(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ti_entry_orders_status
                    ON ti_entry_orders_v3(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ti_entry_orders_reconcile
                    ON ti_entry_orders_v3(mode, updated_at DESC)
                    WHERE status IN (
                        'SUBMITTING', 'OPEN', 'PARTIALLY_FILLED', 'CANCEL_PENDING', 'UNCERTAIN'
                    ) OR COALESCE(json_extract(payload, '$.terminal_notified'), 0) = 0;
                CREATE TABLE IF NOT EXISTS ti_positions_v2 (
                    position_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (plan_id) REFERENCES ti_plans_v2(plan_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_ti_positions_symbol_state
                    ON ti_positions_v2(symbol, state, updated_at DESC);
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
                    (8, "persistent_strategy_review_pipeline", now),
                    (9, "crypto_5m_alpha_factory_and_persistent_live_authorization", now),
                    (10, "event_driven_trading_intelligence_v2", now),
                ],
            )
