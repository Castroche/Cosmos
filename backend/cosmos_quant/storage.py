from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .configuration import UserConfiguration
from .repository_audit import AuditRepository
from .repository_configuration import ConfigurationRepository
from .repository_execution import ExecutionRepository
from .repository_governance import GovernanceRepository
from .repository_learning import LearningRepository
from .repository_market_data import MarketDataRepository
from .repository_migrations import MigrationsRepository
from .repository_research import ResearchRepository
from .repository_strategies import StrategiesRepository


class SqliteStore:
    """Shared WAL, lock and transaction owner with explicit domain delegation."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.migrations = MigrationsRepository(self)
        self.configuration = ConfigurationRepository(self)
        self.research = ResearchRepository(self)
        self.strategies = StrategiesRepository(self)
        self.market_data = MarketDataRepository(self)
        self.governance = GovernanceRepository(self)
        self.audit = AuditRepository(self)
        self.execution = ExecutionRepository(self)
        self._backup_before_schema_upgrade(11)
        self._enable_wal()
        self._initialize()
        self.learning = LearningRepository(self)
        self.learning.initialize()

    def _enable_wal(self) -> None:
        """Set the persistent journal mode once, not on every read connection."""

        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        finally:
            connection.close()

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
            # The explicit v2 cleanup workflow creates and verifies this baseline
            # before schema v10 is installed. Reusing it avoids another multi-GB
            # startup copy while retaining a tested rollback point.
            v2_baselines = list(
                (self.path.parent / "backups").glob(f"{self.path.stem}-pre-v2-upgrade-*.db")
            )
            if target_version == 10 and any(
                candidate.is_file() and candidate.stat().st_size > 0 for candidate in v2_baselines
            ):
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
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def integrity_check(self) -> str:
        # WAL readers see a consistent snapshot without serializing application
        # writes behind a multi-GB scan. Always close this long-lived reader.
        with closing(self.connect()) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"

    def backup_to(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # SQLite's online backup API coordinates its own read snapshot. Holding
        # the application lock here stalls ticker writes, reconciliation and all
        # HTTP handlers that synchronously read configuration or risk state.
        with closing(self.connect()) as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target, pages=256)
                target.commit()
                result = target.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"backup integrity check failed: {result}")
            finally:
                target.close()
        return destination

    def _initialize(self) -> None:
        return self.migrations._initialize()

    def load_configuration(self) -> tuple[UserConfiguration, datetime | None]:
        return self.configuration.load_configuration()

    def save_configuration(self, config: UserConfiguration) -> datetime:
        return self.configuration.save_configuration(config)

    def load_ai_configuration(self) -> tuple[dict | None, datetime | None]:
        return self.configuration.load_ai_configuration()

    def save_ai_configuration(self, config) -> datetime:
        return self.configuration.save_ai_configuration(config)

    def save_ai_provider_runtime(self, provider, state) -> None:
        return self.research.save_ai_provider_runtime(provider, state)

    def load_ai_provider_runtime(self, provider) -> dict | None:
        return self.research.load_ai_provider_runtime(provider)

    def save_ai_usage_event(self, event) -> None:
        return self.configuration.save_ai_usage_event(event)

    def load_ai_usage_events(
        self, *, since: datetime, until: datetime, provider=None, limit: int | None = None
    ) -> list[dict]:
        return self.configuration.load_ai_usage_events(
            since=since, until=until, provider=provider, limit=limit
        )

    def save_commissioning(self, exchange_id: str, market_type: str, result) -> None:
        return self.research.save_commissioning(exchange_id, market_type, result)

    def load_commissioning(self, exchange_id: str, market_type: str) -> dict | None:
        return self.research.load_commissioning(exchange_id, market_type)

    def save_strategy(self, plan) -> None:
        return self.strategies.save_strategy(plan)

    def load_strategies(self) -> list[dict]:
        return self.research.load_strategies()

    def save_backtest(self, plan_id: str, result) -> None:
        return self.research.save_backtest(plan_id, result)

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
        return self.market_data.save_research_dataset(
            dataset,
            candles,
            funding_rates,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )

    def load_research_dataset(self, dataset_id: str) -> dict | None:
        return self.market_data.load_research_dataset(dataset_id)

    def load_research_dataset_candles(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_candles(dataset_id)

    def load_research_dataset_funding(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_funding(dataset_id)

    def load_research_dataset_open_interest(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_open_interest(dataset_id)

    def load_research_dataset_positioning(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_positioning(dataset_id)

    def load_research_dataset_basis(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_basis(dataset_id)

    def load_research_dataset_order_flow(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_order_flow(dataset_id)

    def load_alpha_runtime_feature_tail(
        self, dataset_id: str, *, limit: int = 5000
    ) -> dict[str, list[dict]]:
        return self.research.load_alpha_runtime_feature_tail(dataset_id, limit=limit)

    def load_research_dataset_liquidation_flow(self, dataset_id: str) -> list[dict]:
        return self.market_data.load_research_dataset_liquidation_flow(dataset_id)

    def save_liquidation_event(self, event) -> None:
        return self.market_data.save_liquidation_event(event)

    def list_liquidation_events(
        self, *, symbol: str | None = None, limit: int = 1000
    ) -> list[dict]:
        return self.market_data.list_liquidation_events(symbol=symbol, limit=limit)

    def summarize_liquidation_events(self) -> list[dict]:
        return self.market_data.summarize_liquidation_events()

    def start_market_data_capture_session(self, session) -> None:
        return self.research.start_market_data_capture_session(session)

    def heartbeat_market_data_capture_session(
        self, capture_id: str, heartbeat_at: datetime
    ) -> bool:
        return self.research.heartbeat_market_data_capture_session(capture_id, heartbeat_at)

    def finish_market_data_capture_session(
        self, capture_id: str, *, status: str, ended_at: datetime, error: str = ""
    ) -> bool:
        return self.research.finish_market_data_capture_session(
            capture_id, status=status, ended_at=ended_at, error=error
        )

    def interrupt_open_market_data_capture_sessions(
        self, *, source: str, stream: str, reason: str
    ) -> int:
        return self.research.interrupt_open_market_data_capture_sessions(
            source=source, stream=stream, reason=reason
        )

    def list_market_data_capture_sessions(
        self, *, source: str | None = None, stream: str | None = None, limit: int = 100000
    ) -> list[dict]:
        return self.research.list_market_data_capture_sessions(
            source=source, stream=stream, limit=limit
        )

    def append_market_catalog_journal_observation(self, snapshot, events: list) -> None:
        return self.market_data.append_market_catalog_journal_observation(snapshot, events)

    def load_latest_market_catalog_journal_snapshot(
        self, *, exchange: str, market_type: str
    ) -> dict | None:
        return self.market_data.load_latest_market_catalog_journal_snapshot(
            exchange=exchange, market_type=market_type
        )

    def list_market_catalog_journal_snapshots(
        self, *, exchange: str | None = None, market_type: str | None = None, limit: int = 1000
    ) -> list[dict]:
        return self.market_data.list_market_catalog_journal_snapshots(
            exchange=exchange, market_type=market_type, limit=limit
        )

    def list_market_catalog_change_events(
        self, *, exchange: str | None = None, market_type: str | None = None, limit: int = 1000
    ) -> list[dict]:
        return self.market_data.list_market_catalog_change_events(
            exchange=exchange, market_type=market_type, limit=limit
        )

    def save_official_archive_index(self, archive) -> None:
        return self.market_data.save_official_archive_index(archive)

    def load_official_archive_index(self, archive_id: str) -> dict | None:
        return self.market_data.load_official_archive_index(archive_id)

    def list_official_archive_indexes(
        self, *, exchange: str | None = None, limit: int = 100
    ) -> list[dict]:
        return self.market_data.list_official_archive_indexes(exchange=exchange, limit=limit)

    def save_historical_anchor_scan(self, scan) -> None:
        return self.market_data.save_historical_anchor_scan(scan)

    def load_historical_anchor_scan(self, scan_id: str) -> dict | None:
        return self.market_data.load_historical_anchor_scan(scan_id)

    def list_historical_anchor_scans(
        self, *, exchange: str | None = None, limit: int = 100
    ) -> list[dict]:
        return self.market_data.list_historical_anchor_scans(exchange=exchange, limit=limit)

    def list_research_datasets(self, limit: int = 50) -> list[dict]:
        return self.market_data.list_research_datasets(limit)

    def save_research_job(self, job) -> None:
        return self.research.save_research_job(job)

    def load_research_job(self, job_id: str) -> dict | None:
        return self.research.load_research_job(job_id)

    def list_research_jobs(self, limit: int = 50) -> list[dict]:
        return self.research.list_research_jobs(limit)

    def save_research_campaign(self, campaign) -> None:
        return self.research.save_research_campaign(campaign)

    def load_research_campaign(self, campaign_id: str) -> dict | None:
        return self.research.load_research_campaign(campaign_id)

    def list_research_campaigns(self, limit: int = 20) -> list[dict]:
        return self.research.list_research_campaigns(limit)

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
        return self.research.register_research_trial_family_experiment(
            family_id=family_id,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            engine_version=engine_version,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
        )

    def load_research_trial_family_experiment(
        self, *, family_id: str, experiment_id: str
    ) -> dict | None:
        return self.research.load_research_trial_family_experiment(
            family_id=family_id, experiment_id=experiment_id
        )

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
        return self.research.register_research_campaign_experiment(
            lineage_id=lineage_id,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            engine_version=engine_version,
            research_configuration_fingerprint=research_configuration_fingerprint,
            strategy_fingerprints=strategy_fingerprints,
            blind_set_id=blind_set_id,
            trial_family_id=trial_family_id,
        )

    def mark_research_lineage_blind_opened(
        self, *, lineage_id: str, blind_consumption_id: str
    ) -> None:
        return self.governance.mark_research_lineage_blind_opened(
            lineage_id=lineage_id, blind_consumption_id=blind_consumption_id
        )

    def load_research_campaign_lineage(self, lineage_id: str) -> dict | None:
        return self.research.load_research_campaign_lineage(lineage_id)

    def list_research_campaign_experiments(self, lineage_id: str) -> list[dict]:
        return self.research.list_research_campaign_experiments(lineage_id)

    def save_research_campaign_job(self, job) -> None:
        return self.research.save_research_campaign_job(job)

    def save_strategy_review_job(self, job) -> None:
        return self.strategies.save_strategy_review_job(job)

    def save_strategy_review_job_if_fingerprint_absent(self, job) -> tuple[dict, bool]:
        return self.strategies.save_strategy_review_job_if_fingerprint_absent(job)

    def load_strategy_review_job(self, job_id: str) -> dict | None:
        return self.strategies.load_strategy_review_job(job_id)

    def load_strategy_review_job_by_fingerprint(self, review_fingerprint: str) -> dict | None:
        return self.strategies.load_strategy_review_job_by_fingerprint(review_fingerprint)

    def list_strategy_review_jobs(self, limit: int = 500) -> list[dict]:
        return self.strategies.list_strategy_review_jobs(limit)

    def load_research_campaign_job(self, job_id: str) -> dict | None:
        return self.research.load_research_campaign_job(job_id)

    def list_research_campaign_jobs(self, limit: int = 50) -> list[dict]:
        return self.research.list_research_campaign_jobs(limit)

    def save_research_universe_job(self, job) -> None:
        return self.market_data.save_research_universe_job(job)

    def load_research_universe_job(self, job_id: str) -> dict | None:
        return self.market_data.load_research_universe_job(job_id)

    def list_research_universe_jobs(self, limit: int = 50) -> list[dict]:
        return self.market_data.list_research_universe_jobs(limit)

    def save_research_universe_snapshot(self, snapshot) -> None:
        return self.market_data.save_research_universe_snapshot(snapshot)

    def load_research_universe_snapshot(self, snapshot_id: str) -> dict | None:
        return self.market_data.load_research_universe_snapshot(snapshot_id)

    def list_research_universe_snapshots(self, limit: int = 50) -> list[dict]:
        return self.market_data.list_research_universe_snapshots(limit)

    def load_research_campaign_cell(self, cache_key: str) -> dict | None:
        return self.research.load_research_campaign_cell(cache_key)

    def save_research_campaign_cell(
        self,
        *,
        cache_key: str,
        strategy_fingerprint: str,
        dataset_fingerprint: str,
        engine_version: int,
        cell,
    ) -> None:
        return self.research.save_research_campaign_cell(
            cache_key=cache_key,
            strategy_fingerprint=strategy_fingerprint,
            dataset_fingerprint=dataset_fingerprint,
            engine_version=engine_version,
            cell=cell,
        )

    def reserve_blind_set(
        self, *, blind_set_id: str, strategy_fingerprint: str, evaluation_id: str
    ) -> bool:
        return self.governance.reserve_blind_set(
            blind_set_id=blind_set_id,
            strategy_fingerprint=strategy_fingerprint,
            evaluation_id=evaluation_id,
        )

    def complete_blind_set_consumption(
        self, *, evaluation_id: str, campaign_id: str, passed: bool
    ) -> None:
        return self.governance.complete_blind_set_consumption(
            evaluation_id=evaluation_id, campaign_id=campaign_id, passed=passed
        )

    def load_blind_set_consumption(self, blind_set_id: str) -> dict | None:
        return self.governance.load_blind_set_consumption(blind_set_id)

    def save_relative_value_report(self, report) -> None:
        return self.research.save_relative_value_report(report)

    def load_relative_value_report(self, report_id: str) -> dict | None:
        return self.research.load_relative_value_report(report_id)

    def list_relative_value_reports(self, limit: int = 50) -> list[dict]:
        return self.research.list_relative_value_reports(limit)

    def save_relative_value_campaign(self, report) -> None:
        return self.research.save_relative_value_campaign(report)

    def load_relative_value_campaign(self, campaign_id: str) -> dict | None:
        return self.research.load_relative_value_campaign(campaign_id)

    def list_relative_value_campaigns(self, limit: int = 50) -> list[dict]:
        return self.research.list_relative_value_campaigns(limit)

    def save_carry_report(self, report) -> None:
        return self.research.save_carry_report(report)

    def load_carry_report(self, report_id: str) -> dict | None:
        return self.research.load_carry_report(report_id)

    def list_carry_reports(self, limit: int = 50) -> list[dict]:
        return self.research.list_carry_reports(limit)

    def save_carry_campaign(self, report) -> None:
        return self.research.save_carry_campaign(report)

    def load_carry_campaign(self, campaign_id: str) -> dict | None:
        return self.research.load_carry_campaign(campaign_id)

    def list_carry_campaigns(self, limit: int = 50) -> list[dict]:
        return self.research.list_carry_campaigns(limit)

    def save_strategy_candidate(self, candidate) -> None:
        return self.strategies.save_strategy_candidate(candidate)

    def save_strategy_candidate_if_fingerprint_absent(self, candidate) -> tuple[dict, bool]:
        return self.strategies.save_strategy_candidate_if_fingerprint_absent(candidate)

    def promote_strategy_candidate(self, candidate, retired_candidate=None) -> None:
        return self.strategies.promote_strategy_candidate(candidate, retired_candidate)

    def load_strategy_candidate(self, candidate_id: str) -> dict | None:
        return self.strategies.load_strategy_candidate(candidate_id)

    def load_strategy_candidate_by_fingerprint(self, spec_fingerprint: str) -> dict | None:
        return self.strategies.load_strategy_candidate_by_fingerprint(spec_fingerprint)

    def list_strategy_candidates(self, limit: int = 100) -> list[dict]:
        return self.strategies.list_strategy_candidates(limit)

    def list_strategy_review_candidate_payloads(self, limit: int = 1000) -> list[dict]:
        return self.strategies.list_strategy_review_candidate_payloads(limit)

    def list_strategy_candidate_summaries(self, limit: int = 500) -> list[dict]:
        return self.strategies.list_strategy_candidate_summaries(limit)

    def save_personal_strategy_library_item(self, item) -> None:
        return self.strategies.save_personal_strategy_library_item(item)

    def load_personal_strategy_library_item(self, item_id: str) -> dict | None:
        return self.strategies.load_personal_strategy_library_item(item_id)

    def list_personal_strategy_library_items(self, limit: int = 500) -> list[dict]:
        return self.strategies.list_personal_strategy_library_items(limit)

    def delete_personal_strategy_library_item(self, item_id: str) -> bool:
        return self.strategies.delete_personal_strategy_library_item(item_id)

    def set_strategy_library_hidden(self, item_id: str, hidden: bool) -> None:
        return self.strategies.set_strategy_library_hidden(item_id, hidden)

    def hidden_strategy_library_items(self) -> set[str]:
        return self.strategies.hidden_strategy_library_items()

    def save_agent_launch_profile(self, profile) -> None:
        return self.research.save_agent_launch_profile(profile)

    def load_agent_launch_profile(self, profile_id: str) -> dict | None:
        return self.research.load_agent_launch_profile(profile_id)

    def list_agent_launch_profiles(self, limit: int = 100) -> list[dict]:
        return self.research.list_agent_launch_profiles(limit)

    def retire_superseded_system_candidate(self, candidate_id: str) -> bool:
        return self.strategies.retire_superseded_system_candidate(candidate_id)

    def load_latest_backtest(self, plan_id: str) -> dict | None:
        return self.research.load_latest_backtest(plan_id)

    def save_risk_state(self, payload: str) -> None:
        return self.audit.save_risk_state(payload)

    def load_risk_state(self) -> str | None:
        return self.audit.load_risk_state()

    def save_order_record(self, exchange_id: str, record) -> None:
        return self.execution.save_order_record(exchange_id, record)

    def load_order_records(self, exchange_id: str) -> list[dict]:
        return self.execution.load_order_records(exchange_id)

    def save_multi_leg_execution_run(self, run) -> None:
        return self.research.save_multi_leg_execution_run(run)

    def load_multi_leg_execution_run(self, run_id: str) -> dict | None:
        return self.research.load_multi_leg_execution_run(run_id)

    def list_multi_leg_execution_runs(self, limit: int = 100) -> list[dict]:
        return self.research.list_multi_leg_execution_runs(limit)

    def save_multi_leg_commissioning(self, result) -> None:
        return self.research.save_multi_leg_commissioning(result)

    def load_multi_leg_commissioning(self, exchange_id: str, strategy_kind: str) -> dict | None:
        return self.research.load_multi_leg_commissioning(exchange_id, strategy_kind)

    def list_multi_leg_commissioning(self) -> list[dict]:
        return self.research.list_multi_leg_commissioning()

    def daily_equity_baseline(self, account_id: str, current_equity) -> str:
        return self.research.daily_equity_baseline(account_id, current_equity)

    def append_audit(self, event) -> None:
        return self.audit.append_audit(event)

    def load_audit(self, limit: int = 100) -> list[dict]:
        return self.audit.load_audit(limit)

    def page_audit(
        self,
        *,
        limit: int = 50,
        before_sequence: int | None = None,
        category: str | None = None,
        severity: str | None = None,
        search: str | None = None,
    ) -> tuple[list[dict], int | None]:
        return self.audit.page_audit(
            limit=limit,
            before_sequence=before_sequence,
            category=category,
            severity=severity,
            search=search,
        )

    def save_candles(self, exchange_id: str, symbol: str, timeframe: str, candles) -> None:
        return self.market_data.save_candles(exchange_id, symbol, timeframe, candles)

    def load_candles(
        self, exchange_id: str, symbol: str, timeframe: str, limit: int = 500
    ) -> list[dict]:
        return self.market_data.load_candles(exchange_id, symbol, timeframe, limit)

    def save_paper_state(self, account_id: str, payload: str) -> None:
        return self.research.save_paper_state(account_id, payload)

    def load_paper_state(self, account_id: str) -> str | None:
        return self.research.load_paper_state(account_id)

    def reserve_research_trial(self, trial) -> dict:
        return self.research.reserve_research_trial(trial)

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
        return self.research.append_research_trial_result(
            trial_id,
            result=result,
            p_value=p_value,
            adjusted_p_value=adjusted_p_value,
            dsr=dsr,
            pbo=pbo,
            global_alpha=global_alpha,
            family_alpha=family_alpha,
            reason_codes=reason_codes,
        )

    def load_research_trial(self, trial_id: str) -> dict | None:
        return self.research.load_research_trial(trial_id)

    def list_research_trials(self, *, family_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.research.list_research_trials(family_id=family_id, limit=limit)

    def allocate_online_fdr(
        self, *, stream_id: str, scope: str, target_fdr: Decimal, idempotency_key: str
    ) -> dict:
        return self.research.allocate_online_fdr(
            stream_id=stream_id, scope=scope, target_fdr=target_fdr, idempotency_key=idempotency_key
        )

    def record_online_fdr_result(
        self, *, stream_id: str, sequence: int, idempotency_key: str, p_value: Decimal
    ) -> dict:
        return self.research.record_online_fdr_result(
            stream_id=stream_id, sequence=sequence, idempotency_key=idempotency_key, p_value=p_value
        )

    def load_online_fdr_snapshot(self, stream_id: str) -> dict | None:
        return self.research.load_online_fdr_snapshot(stream_id)

    def restore_online_fdr_snapshot(self, payload: dict) -> None:
        return self.research.restore_online_fdr_snapshot(payload)

    def save_research_hypothesis_v2(self, hypothesis, *, parent_id: str | None = None) -> dict:
        return self.research.save_research_hypothesis_v2(hypothesis, parent_id=parent_id)

    def create_autonomous_research_event(
        self, *, run_id: str, idempotency_key: str, snapshot_ready_at: datetime, payload: dict
    ) -> dict:
        return self.research.create_autonomous_research_event(
            run_id=run_id,
            idempotency_key=idempotency_key,
            snapshot_ready_at=snapshot_ready_at,
            payload=payload,
        )

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
        return self.research.consume_autonomous_research_budget(
            research_event_id=research_event_id,
            family_id=family_id,
            budget_kind=budget_kind,
            amount=amount,
            limit=limit,
            idempotency_key=idempotency_key,
        )

    def commit_autonomous_research_decision(
        self, result, *, trial_result: dict | None = None
    ) -> dict:
        return self.research.commit_autonomous_research_decision(result, trial_result=trial_result)

    def load_autonomous_research_event(self, run_id: str) -> dict | None:
        return self.research.load_autonomous_research_event(run_id)

    def put_research_artifact(
        self, *, artifact_fingerprint: str, artifact_kind: str, payload: dict
    ) -> None:
        return self.research.put_research_artifact(
            artifact_fingerprint=artifact_fingerprint, artifact_kind=artifact_kind, payload=payload
        )

    def load_research_artifact(self, artifact_fingerprint: str, artifact_kind: str) -> dict | None:
        return self.research.load_research_artifact(artifact_fingerprint, artifact_kind)

    def register_behavior_fingerprint(
        self, *, behavior_fingerprint: str, ir_hash: str, family_id: str, payload: dict
    ) -> bool:
        return self.research.register_behavior_fingerprint(
            behavior_fingerprint=behavior_fingerprint,
            ir_hash=ir_hash,
            family_id=family_id,
            payload=payload,
        )

    def register_research_novelty(
        self, *, novelty_kind: str, novelty_fingerprint: str, family_id: str, payload: dict
    ) -> bool:
        return self.research.register_research_novelty(
            novelty_kind=novelty_kind,
            novelty_fingerprint=novelty_fingerprint,
            family_id=family_id,
            payload=payload,
        )

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
        return self.strategies.save_candidate_lineage_v2(
            lineage_id=lineage_id,
            research_event_id=research_event_id,
            family_id=family_id,
            candidate_id=candidate_id,
            parent_ids=parent_ids,
            operation=operation,
            payload=payload,
        )

    def save_generator_envelope(self, envelope) -> dict:
        return self.research.save_generator_envelope(envelope)

    def load_generator_envelope(self, envelope_id: str) -> dict | None:
        return self.research.load_generator_envelope(envelope_id)

    def save_research_certificate(self, certificate, certificate_type: str) -> dict:
        return self.governance.save_research_certificate(certificate, certificate_type)

    def revoke_research_certificate(self, event) -> dict:
        return self.governance.revoke_research_certificate(event)

    def load_research_certificate_status(self, certificate_id: str) -> dict | None:
        return self.governance.load_research_certificate_status(certificate_id)

    def save_alpha_model_artifact(self, artifact) -> dict:
        return self.research.save_alpha_model_artifact(artifact)

    def load_alpha_model_artifact(self, model_id: str) -> dict | None:
        return self.research.load_alpha_model_artifact(model_id)

    def list_research_certificates_by_type(self, certificate_type: str) -> list[dict]:
        return self.governance.list_research_certificates_by_type(certificate_type)

    def save_sandbox_admission_handoff(
        self, certificate, *, status: str, details: dict | None = None
    ) -> dict:
        return self.governance.save_sandbox_admission_handoff(
            certificate, status=status, details=details
        )

    def load_sandbox_admission_handoff(self, certificate_id: str) -> dict | None:
        return self.governance.load_sandbox_admission_handoff(certificate_id)

    def list_sandbox_admission_handoffs(self, *, limit: int = 200) -> list[dict]:
        return self.governance.list_sandbox_admission_handoffs(limit=limit)

    def save_sandbox_execution_evidence(self, evidence) -> dict:
        return self.governance.save_sandbox_execution_evidence(evidence)

    def list_sandbox_execution_evidence(self, certificate_id: str) -> list[dict]:
        return self.governance.list_sandbox_execution_evidence(certificate_id)

    def find_completed_blind_consumption(self, strategy_fingerprint: str) -> dict | None:
        return self.governance.find_completed_blind_consumption(strategy_fingerprint)

    def list_full_strategy_certificates_v2(self) -> list[dict]:
        return self.governance.list_full_strategy_certificates_v2()

    def save_live_strategy_selection(self, selection) -> dict:
        return self.governance.save_live_strategy_selection(selection)

    def load_live_strategy_selection(self, certificate_id: str) -> dict | None:
        return self.governance.load_live_strategy_selection(certificate_id)

    def save_persistent_live_authorization(self, grant, *, event_type: str) -> dict:
        return self.governance.save_persistent_live_authorization(grant, event_type=event_type)

    def load_persistent_live_authorization(self) -> dict | None:
        return self.governance.load_persistent_live_authorization()
