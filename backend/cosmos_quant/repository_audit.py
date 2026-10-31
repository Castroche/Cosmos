from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime


class AuditRepository:
    def __init__(self, store):
        self.store = store

    def save_risk_state(self, payload: str) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM risk_control_state WHERE id = 1"
            ).fetchone()
        return row["payload"] if row else None

    def append_audit(self, event) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
