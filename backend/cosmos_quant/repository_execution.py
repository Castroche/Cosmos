from __future__ import annotations

import json


class ExecutionRepository:
    def __init__(self, store):
        self.store = store

    def save_order_record(self, exchange_id: str, record) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM order_ledger
                WHERE exchange_id = ? ORDER BY updated_at DESC
                """,
                (exchange_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]
