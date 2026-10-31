from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from .strategy_spec import validate_strategy_spec
from .text_encoding import repair_text_tree


class StrategiesRepository:
    def __init__(self, store):
        self.store = store

    def save_strategy(self, plan) -> None:
        now = datetime.now(UTC).isoformat()
        payload = plan.model_dump_json()
        with self.store._lock, self.store.connect() as connection:
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

    def save_strategy_review_job(self, job) -> None:
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_review_jobs
                    (job_id, candidate_id, review_fingerprint, status, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    job.id,
                    job.candidate_id,
                    job.review_fingerprint,
                    str(job.status),
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )

    def save_strategy_review_job_if_fingerprint_absent(
        self,
        job,
    ) -> tuple[dict, bool]:
        """Atomically enqueue one review for an immutable review fingerprint."""

        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM strategy_review_jobs WHERE review_fingerprint = ?",
                (job.review_fingerprint,),
            ).fetchone()
            if existing:
                connection.commit()
                return json.loads(existing["payload"]), False
            connection.execute(
                """
                INSERT INTO strategy_review_jobs
                    (job_id, candidate_id, review_fingerprint, status, payload,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.candidate_id,
                    job.review_fingerprint,
                    str(job.status),
                    job.model_dump_json(),
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            connection.commit()
        return job.model_dump(mode="json"), True

    def load_strategy_review_job(self, job_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM strategy_review_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def load_strategy_review_job_by_fingerprint(
        self,
        review_fingerprint: str,
    ) -> dict | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM strategy_review_jobs WHERE review_fingerprint = ?",
                (review_fingerprint,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_strategy_review_jobs(self, limit: int = 500) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM strategy_review_jobs
                ORDER BY created_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_strategy_candidate(self, candidate) -> None:
        actual_fingerprint = validate_strategy_spec(candidate.spec).fingerprint
        if actual_fingerprint != candidate.spec_fingerprint:
            raise ValueError("strategy candidate fingerprint does not match payload")
        with self.store._lock, self.store.connect() as connection:
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

    def save_strategy_candidate_if_fingerprint_absent(
        self,
        candidate,
    ) -> tuple[dict, bool]:
        """Atomically persist one canonical candidate per strategy fingerprint."""

        actual_fingerprint = validate_strategy_spec(candidate.spec).fingerprint
        if actual_fingerprint != candidate.spec_fingerprint:
            raise ValueError("strategy candidate fingerprint does not match payload")
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT payload FROM strategy_candidates
                WHERE spec_fingerprint = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (candidate.spec_fingerprint,),
            ).fetchone()
            if existing:
                connection.commit()
                return repair_text_tree(json.loads(existing["payload"])), False
            connection.execute(
                """
                INSERT INTO strategy_candidates
                    (candidate_id, spec_fingerprint, family, status, source,
                     payload, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
        return candidate.model_dump(mode="json"), True

    def promote_strategy_candidate(
        self,
        candidate,
        retired_candidate=None,
    ) -> None:
        family = candidate.spec["family"]
        with self.store._lock, self.store.connect() as connection:
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
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM strategy_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def load_strategy_candidate_by_fingerprint(
        self,
        spec_fingerprint: str,
    ) -> dict | None:
        """Return the canonical newest candidate for an immutable strategy spec."""

        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM strategy_candidates
                WHERE spec_fingerprint = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (spec_fingerprint,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_strategy_candidates(self, limit: int = 100) -> list[dict]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM strategy_candidates
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def list_strategy_review_candidate_payloads(self, limit: int = 1000) -> list[dict]:
        """Keep legacy counting without decoding its full backtest evidence every poll."""

        with closing(self.store.connect()) as connection:
            rows = connection.execute(
                """
                SELECT CASE
                    WHEN json_type(payload, '$.research_context') = 'object' THEN payload
                    ELSE json_object('id', candidate_id)
                END AS payload
                FROM strategy_candidates ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def list_strategy_candidate_summaries(self, limit: int = 500) -> list[dict]:
        """Load only fields needed by operator overview screens.

        Candidate payloads contain full research evidence and can be hundreds of
        kilobytes each.  Parsing every payload just to render a name and status
        made the review workspace take several seconds to open.
        """

        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    candidate_id,
                    status,
                    COALESCE(
                        json_extract(payload, '$.spec.name'),
                        json_extract(payload, '$.spec.label'),
                        candidate_id
                    ) AS display_name,
                    updated_at
                FROM strategy_candidates
                ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_personal_strategy_library_item(self, item) -> None:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        now = str(payload["updated_at"])
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM personal_strategy_library WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        return repair_text_tree(json.loads(row["payload"])) if row else None

    def list_personal_strategy_library_items(self, limit: int = 500) -> list[dict]:
        bounded = min(max(limit, 1), 1000)
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM personal_strategy_library
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [repair_text_tree(json.loads(row["payload"])) for row in rows]

    def delete_personal_strategy_library_item(self, item_id: str) -> bool:
        with self.store._lock, self.store.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM personal_strategy_library WHERE item_id = ?",
                (item_id,),
            ).rowcount
        return bool(deleted)

    def set_strategy_library_hidden(self, item_id: str, hidden: bool) -> None:
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                "SELECT item_id FROM strategy_library_preferences WHERE hidden = 1"
            ).fetchall()
        return {str(row["item_id"]) for row in rows}

    def retire_superseded_system_candidate(self, candidate_id: str) -> bool:
        """Archive an obsolete built-in even when its legacy spec no longer validates."""

        reasons = [
            "该内置模板版本已被当前公开策略库替代并自动归档",
            "历史候选仅保留审计记录，不再参与研究排序或执行",
        ]
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
