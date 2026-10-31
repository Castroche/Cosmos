from __future__ import annotations

import json
from datetime import UTC, datetime


class MarketDataRepository:
    def __init__(self, store):
        self.store = store

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

        with self.store._lock, self.store.connect() as connection:
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
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_research_dataset_candles(self, dataset_id: str) -> list[dict]:
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def append_market_catalog_journal_observation(
        self,
        snapshot,
        events: list,
    ) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM official_archive_indexes {where}
                ORDER BY retrieved_at DESC, archive_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_historical_anchor_scan(self, scan) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload FROM historical_anchor_scans {where}
                ORDER BY anchor_month DESC, retrieved_at DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def list_research_datasets(self, limit: int = 50) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_datasets
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_research_universe_job(self, job) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_universe_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_universe_jobs(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM research_universe_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_research_universe_snapshots(self, limit: int = 50) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM research_universe_snapshots
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
