from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from .text_encoding import repair_text_tree


class ResearchRepository:
    def __init__(self, store):
        self.store = store

    def save_ai_provider_runtime(self, provider, state) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM ai_provider_runtime WHERE provider = ?",
                (str(provider),),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_commissioning(self, exchange_id: str, market_type: str, result) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM exchange_commissioning
                WHERE exchange_id = ? AND market_type = ?
                """,
                (exchange_id, market_type),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_strategies(self) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM strategy_plans ORDER BY updated_at DESC"
            ).fetchall()
        strategies = []
        for row in rows:
            payload = json.loads(row["payload"])
            repaired = repair_text_tree(payload)
            strategies.append(repaired)
            if repaired != payload:
                with self.store._lock, self.store.connect() as connection:
                    connection.execute(
                        "UPDATE strategy_plans SET payload = ? WHERE id = ?",
                        (json.dumps(repaired, ensure_ascii=False), repaired["id"]),
                    )
        return strategies

    def save_backtest(self, plan_id: str, result) -> None:
        if not result.strategy_fingerprint or not result.data_fingerprint:
            raise ValueError("backtest provenance is required")
        with self.store._lock, self.store.connect() as connection:
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

    def load_alpha_runtime_feature_tail(
        self,
        dataset_id: str,
        *,
        limit: int = 5_000,
    ) -> dict[str, list[dict]]:
        """Load a bounded, ordered point-in-time feature window for 5m inference.

        Runtime inference must never deserialize an entire two-year feature cube on
        the decision path.  Every source table is append-only and keyed by
        ``(dataset_id, sequence)``, so reading the newest bounded suffix preserves
        causal ordering while keeping the 5m path predictable.
        """

        bounded = min(max(int(limit), 1), 20_000)
        sources = {
            "funding": (
                "research_dataset_funding",
                "timestamp, event_time, available_time, source_time, ingestion_time, "
                "revision_policy, rate, mark_price",
            ),
            "open_interest": (
                "research_dataset_open_interest",
                "timestamp, event_time, available_time, source_time, ingestion_time, "
                "revision_policy, open_interest_amount, open_interest_value",
            ),
            "positioning": (
                "research_dataset_positioning",
                "timestamp, event_time, available_time, source_time, ingestion_time, "
                "revision_policy, long_short_ratio, long_account_fraction, "
                "short_account_fraction, source_variant",
            ),
            "basis": (
                "research_dataset_basis",
                "timestamp, event_time, available_time, source_time, ingestion_time, "
                "revision_policy, mark_price, index_price, basis_rate",
            ),
            "order_flow": (
                "research_dataset_order_flow",
                "timestamp, event_time, available_time, source_time, ingestion_time, "
                "revision_policy, aggregate_count, underlying_trade_count, base_volume, "
                "quote_volume, taker_buy_base_volume, taker_sell_base_volume, "
                "delta_base_volume, maximum_trade_quote",
            ),
        }
        result: dict[str, list[dict]] = {}
        with self.store.connect() as connection:
            for name, (table, columns) in sources.items():
                rows = connection.execute(
                    f"""
                    SELECT {columns} FROM (
                        SELECT sequence, {columns}
                        FROM {table}
                        WHERE dataset_id = ?
                        ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence
                    """,
                    (dataset_id, bounded),
                ).fetchall()
                result[name] = [dict(row) for row in rows]
            liquidation_rows = connection.execute(
                """
                SELECT payload, event_time, available_time, source_time,
                       ingestion_time, revision_policy FROM (
                    SELECT sequence, payload, event_time, available_time, source_time,
                           ingestion_time, revision_policy
                    FROM research_dataset_liquidation_flow
                    WHERE dataset_id = ?
                    ORDER BY sequence DESC LIMIT ?
                ) ORDER BY sequence
                """,
                (dataset_id, bounded),
            ).fetchall()
            liquidation: list[dict] = []
            for row in liquidation_rows:
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
                liquidation.append(payload)
            result["liquidation_flow"] = liquidation
        return result

    def start_market_data_capture_session(self, session) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def save_research_job(self, job) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_jobs(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_jobs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_campaign(self, campaign) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_campaigns(self, limit: int = 20) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

        with self.store._lock, self.store.connect() as connection:
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
            family_summary = self.store.register_research_trial_family_experiment(
                family_id=trial_family_id,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                engine_version=engine_version,
                research_configuration_fingerprint=research_configuration_fingerprint,
                strategy_fingerprints=fingerprints,
            )
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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

    def load_research_campaign_lineage(self, lineage_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_campaign_lineages WHERE lineage_id = ?",
                (lineage_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_research_campaign_experiments(self, lineage_id: str) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_campaign_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_campaign_jobs(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_campaign_jobs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def load_research_campaign_cell(self, cache_key: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def save_relative_value_report(self, report) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM relative_value_reports
                WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_relative_value_reports(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM relative_value_reports
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_relative_value_campaign(self, report) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM relative_value_campaigns
                WHERE campaign_id = ?
                """,
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_relative_value_campaigns(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM relative_value_campaigns
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_carry_report(self, report) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM carry_reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_carry_reports(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM carry_reports
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_carry_campaign(self, report) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM carry_campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_carry_campaigns(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM carry_campaigns
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_agent_launch_profile(self, profile) -> None:
        payload = (
            profile.model_dump(mode="json") if hasattr(profile, "model_dump") else dict(profile)
        )
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM agent_launch_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_agent_launch_profiles(self, limit: int = 100) -> list[dict]:
        bounded = min(max(limit, 1), 200)
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM agent_launch_profiles
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def load_latest_backtest(self, plan_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM strategy_backtests
                WHERE plan_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_multi_leg_execution_run(self, run) -> None:
        now = run.updated_at.isoformat()
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM multi_leg_execution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_multi_leg_execution_runs(self, limit: int = 100) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM multi_leg_execution_runs
                ORDER BY updated_at DESC LIMIT ?
                """,
                (min(max(limit, 1), 10000),),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_multi_leg_commissioning(self, result) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM multi_leg_commissioning
                WHERE exchange_id = ? AND strategy_kind = ?
                """,
                (exchange_id, strategy_kind),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_multi_leg_commissioning(self) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM multi_leg_commissioning
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def daily_equity_baseline(self, account_id: str, current_equity) -> str:
        today = datetime.now(UTC).date().isoformat()
        with self.store._lock, self.store.connect() as connection:
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

    def save_paper_state(self, account_id: str, payload: str) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM paper_account_state WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return row["payload"] if row else None

    def reserve_research_trial(self, trial) -> dict:
        """Persist an immutable trial before any return-bearing evidence is read."""

        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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
        loaded = self.store.load_research_trial(trial_id)
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
        with self.store._lock, self.store.connect() as connection:
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
        loaded = self.store.load_research_trial(trial_id)
        if loaded is None:
            raise RuntimeError("completed research trial could not be reloaded")
        return loaded

    def load_research_trial(self, trial_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
            if (loaded := self.store.load_research_trial(row["trial_id"])) is not None
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def save_generator_envelope(self, envelope) -> dict:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM generator_envelopes WHERE envelope_id = ?",
                (envelope_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_alpha_model_artifact(self, artifact) -> dict:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT fingerprint FROM alpha_model_artifacts_v1 WHERE model_id = ?",
                (artifact.id,),
            ).fetchone()
            if existing and existing["fingerprint"] != artifact.fingerprint:
                raise RuntimeError("immutable alpha model id collision")
            connection.execute(
                """
                INSERT OR IGNORE INTO alpha_model_artifacts_v1
                    (model_id, profile, model_family, fingerprint, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.profile,
                    artifact.model_family,
                    artifact.fingerprint,
                    artifact.model_dump_json(),
                    artifact.created_at.isoformat(),
                ),
            )
            connection.commit()
        return artifact.model_dump(mode="json")

    def load_alpha_model_artifact(self, model_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM alpha_model_artifacts_v1 WHERE model_id = ?",
                (model_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None
