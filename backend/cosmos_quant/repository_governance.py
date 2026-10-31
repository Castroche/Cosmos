from __future__ import annotations

import json
from datetime import UTC, datetime


class GovernanceRepository:
    def __init__(self, store):
        self.store = store

    def mark_research_lineage_blind_opened(
        self,
        *,
        lineage_id: str,
        blind_consumption_id: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store._lock, self.store.connect() as connection:
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

    def reserve_blind_set(
        self,
        *,
        blind_set_id: str,
        strategy_fingerprint: str,
        evaluation_id: str,
    ) -> bool:
        """Atomically break a one-time blind seal before any holdout data is loaded."""

        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def save_research_certificate(self, certificate, certificate_type: str) -> dict:
        strategy_ir_hash = getattr(certificate, "strategy_ir_hash", "")
        created_at = getattr(certificate, "issued_at", None) or getattr(
            certificate, "created_at", datetime.now(UTC)
        )
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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
        with self.store._lock, self.store.connect() as connection:
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

    def list_research_certificates_by_type(self, certificate_type: str) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.payload,
                       CASE WHEN r.certificate_id IS NULL THEN 0 ELSE 1 END AS revoked
                FROM research_certificates_v2 c
                LEFT JOIN (
                    SELECT DISTINCT certificate_id FROM certificate_revocations
                ) r ON r.certificate_id = c.certificate_id
                WHERE c.certificate_type = ?
                ORDER BY c.created_at DESC
                """,
                (certificate_type,),
            ).fetchall()
        return [
            {"certificate": json.loads(row["payload"]), "revoked": bool(row["revoked"])}
            for row in rows
        ]

    def save_sandbox_admission_handoff(
        self,
        certificate,
        *,
        status: str,
        details: dict | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        payload = {
            "certificate_id": certificate.id,
            "exchange": certificate.exchange,
            "trading_mode": certificate.trading_mode,
            "status": status,
            "maximum_risk_per_trade": str(certificate.maximum_risk_per_trade),
            "maximum_active_instances": certificate.maximum_active_instances,
            "allow_scale_in": certificate.allow_scale_in,
            "allow_pyramiding": certificate.allow_pyramiding,
            "allow_automatic_leverage_increase": (certificate.allow_automatic_leverage_increase),
            **(details or {}),
        }
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO sandbox_admission_handoffs_v1
                    (certificate_id, exchange_id, trading_mode, status,
                     payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(certificate_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    certificate.id,
                    certificate.exchange,
                    certificate.trading_mode,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return payload

    def load_sandbox_admission_handoff(self, certificate_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM sandbox_admission_handoffs_v1 WHERE certificate_id = ?",
                (certificate_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_sandbox_admission_handoffs(self, *, limit: int = 200) -> list[dict]:
        bounded = min(max(int(limit), 1), 2_000)
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM sandbox_admission_handoffs_v1
                ORDER BY updated_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_sandbox_execution_evidence(self, evidence) -> dict:
        with self.store._lock, self.store.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO sandbox_execution_evidence_v1
                    (evidence_id, certificate_id, episode_id, aligned, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.id,
                    evidence.sandbox_certificate_id,
                    evidence.episode_id,
                    int(evidence.aligned),
                    evidence.model_dump_json(),
                    evidence.recorded_at.isoformat(),
                ),
            )
        return evidence.model_dump(mode="json")

    def list_sandbox_execution_evidence(self, certificate_id: str) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM sandbox_execution_evidence_v1
                WHERE certificate_id = ? ORDER BY created_at
                """,
                (certificate_id,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def find_completed_blind_consumption(self, strategy_fingerprint: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT blind_set_id, strategy_fingerprint, evaluation_id,
                       status, campaign_id, passed, opened_at, completed_at
                FROM blind_set_consumptions
                WHERE strategy_fingerprint = ? AND status = 'completed'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (strategy_fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def list_full_strategy_certificates_v2(self) -> list[dict]:
        with self.store._lock, self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.payload,
                       CASE WHEN r.certificate_id IS NULL THEN 0 ELSE 1 END AS revoked,
                       s.payload AS selection_payload
                FROM research_certificates_v2 c
                LEFT JOIN (
                    SELECT DISTINCT certificate_id FROM certificate_revocations
                ) r ON r.certificate_id = c.certificate_id
                LEFT JOIN live_strategy_selections_v2 s
                    ON s.certificate_id = c.certificate_id
                WHERE c.certificate_type = 'full_strategy_v2'
                ORDER BY c.created_at DESC
                """
            ).fetchall()
        return [
            {
                "certificate": json.loads(row["payload"]),
                "revoked": bool(row["revoked"]),
                "selection": (
                    json.loads(row["selection_payload"]) if row["selection_payload"] else None
                ),
            }
            for row in rows
        ]

    def save_live_strategy_selection(self, selection) -> dict:
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = connection.execute(
                """
                SELECT c.certificate_type,
                       EXISTS(
                           SELECT 1 FROM certificate_revocations r
                           WHERE r.certificate_id = c.certificate_id
                       ) AS revoked
                FROM research_certificates_v2 c WHERE c.certificate_id = ?
                """,
                (selection.certificate_id,),
            ).fetchone()
            if not status or status["certificate_type"] != "full_strategy_v2":
                raise LookupError("valid FullStrategyCertificateV2 is required")
            if status["revoked"]:
                raise RuntimeError("revoked strategy certificate cannot be selected")
            connection.execute(
                """
                INSERT INTO live_strategy_selections_v2
                    (certificate_id, selected, certificate_fingerprint, payload, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(certificate_id) DO UPDATE SET
                    selected = excluded.selected,
                    certificate_fingerprint = excluded.certificate_fingerprint,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    selection.certificate_id,
                    int(selection.selected),
                    selection.certificate_fingerprint,
                    selection.model_dump_json(),
                    selection.updated_at.isoformat(),
                ),
            )
            connection.commit()
        return selection.model_dump(mode="json")

    def load_live_strategy_selection(self, certificate_id: str) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM live_strategy_selections_v2 WHERE certificate_id = ?",
                (certificate_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_persistent_live_authorization(self, grant, *, event_type: str) -> dict:
        now = datetime.now(UTC)
        event_id = f"live_auth_event_{now.strftime('%Y%m%d%H%M%S%f')}"
        payload = grant.model_dump_json()
        with self.store._lock, self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO persistent_live_authorization_v1
                    (id, grant_id, enabled, scope_fingerprint,
                     configuration_fingerprint, payload, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    grant_id = excluded.grant_id,
                    enabled = excluded.enabled,
                    scope_fingerprint = excluded.scope_fingerprint,
                    configuration_fingerprint = excluded.configuration_fingerprint,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    grant.id,
                    int(grant.enabled),
                    grant.scope_fingerprint,
                    grant.configuration_fingerprint,
                    payload,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO live_authorization_events_v1
                    (event_id, grant_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, grant.id, event_type, payload, now.isoformat()),
            )
            connection.commit()
        return grant.model_dump(mode="json")

    def load_persistent_live_authorization(self) -> dict | None:
        with self.store._lock, self.store.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM persistent_live_authorization_v1 WHERE id = 1"
            ).fetchone()
        return json.loads(row["payload"]) if row else None
