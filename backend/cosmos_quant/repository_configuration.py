from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from .configuration import UserConfiguration
from .text_encoding import repair_text_tree


class ConfigurationRepository:
    def __init__(self, store):
        self.store = store

    def load_configuration(self) -> tuple[UserConfiguration, datetime | None]:
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                "SELECT payload, updated_at FROM app_configuration WHERE id = 1"
            ).fetchone()
        if not row:
            return UserConfiguration(), None
        payload = json.loads(row["payload"])
        repaired = repair_text_tree(payload)
        if repaired != payload:
            with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload, updated_at FROM ai_configuration WHERE id = 1"
            ).fetchone()
        if not row:
            return None, None
        return json.loads(row["payload"]), datetime.fromisoformat(row["updated_at"])

    def save_ai_configuration(self, config) -> datetime:
        now = datetime.now(UTC)
        with self.store._lock, self.store.connect() as connection:
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

    def save_ai_usage_event(self, event) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [json.loads(row["payload"]) for row in rows]
