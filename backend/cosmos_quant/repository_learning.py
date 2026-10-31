"""Learning evidence and shadow quotas share the account SQLite transaction owner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class LearningRepository:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store._lock, self.store.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS ti_learning_evidence (
                    campaign_id TEXT PRIMARY KEY, available_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ti_learning_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ti_learning_shadows (
                    decision_id TEXT PRIMARY KEY, budget_day TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
                    reserved INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ti_learning_shadow_day
                    ON ti_learning_shadows(budget_day, reserved);
                INSERT OR IGNORE INTO schema_migrations(version,name,applied_at)
                    VALUES(11,'learning evidence and isolated shadow comparisons',datetime('now'));
            """)

    def evidence(self, record, now):
        with self.store._lock, self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT available_at FROM ti_learning_evidence WHERE campaign_id=?",
                (record["campaign_id"],),
            ).fetchone()
            # A correction must not become available at the original observation time.
            previous = c.execute(
                "SELECT payload FROM ti_learning_evidence WHERE campaign_id=?",
                (record["campaign_id"],),
            ).fetchone()
            unchanged = previous and json.loads(previous["payload"]) == record
            available = old["available_at"] if unchanged else now.isoformat()
            c.execute(
                "INSERT OR REPLACE INTO ti_learning_evidence VALUES(?,?,?)",
                (record["campaign_id"], available, json.dumps(record, sort_keys=True)),
            )
        return {**record, "available_at": available, "time_basis": "first_observed_complete"}

    def settings(self):
        with self.store.connect() as c:
            row = c.execute("SELECT payload FROM ti_learning_settings WHERE id=1").fetchone()
        return (
            json.loads(row["payload"])
            if row
            else {"enabled": True, "pinned_version": None, "shadow_enabled": True}
        )

    def save_settings(self, payload):
        with self.store._lock, self.store.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO ti_learning_settings VALUES(1,?)", (json.dumps(payload),)
            )
        return payload

    def reserve_shadow(self, decision_id, payload, *, now=None, skip=None):
        now = now or datetime.now(UTC)
        day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.store._lock, self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute(
                "SELECT 1 FROM ti_learning_shadows WHERE decision_id=?", (decision_id,)
            ).fetchone():
                return False
            count = c.execute(
                "SELECT COUNT(*) FROM ti_learning_shadows WHERE budget_day=? AND reserved=1", (day,)
            ).fetchone()[0]
            reason = skip or ("DAILY_LIMIT" if count >= 10 else None)
            reserved = reason is None
            status = "QUEUED" if reserved else "SKIPPED"
            c.execute(
                "INSERT INTO ti_learning_shadows VALUES(?,?,?,?,?,?)",
                (
                    decision_id,
                    day,
                    status,
                    now.isoformat(),
                    json.dumps({**payload, "skip_reason": reason}, default=str),
                    int(reserved),
                ),
            )
        return reserved

    def update_shadow(self, decision_id, status, **changes):
        with self.store._lock, self.store.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload FROM ti_learning_shadows WHERE decision_id=?", (decision_id,)
            ).fetchone()
            if row:
                payload = {**json.loads(row["payload"]), **changes}
                c.execute(
                    "UPDATE ti_learning_shadows SET status=?,payload=? WHERE decision_id=?",
                    (status, json.dumps(payload, default=str), decision_id),
                )

    def shadows(self, limit=50, offset=0):
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT * FROM ti_learning_shadows ORDER BY created_at DESC, "
                "decision_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = c.execute("SELECT COUNT(*) FROM ti_learning_shadows").fetchone()[0]
        return {
            "items": [{**dict(r), "payload": json.loads(r["payload"])} for r in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def summary(self, now=None):
        now = now or datetime.now(UTC)
        day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.store.connect() as c:
            rows = c.execute(
                "SELECT status,COUNT(*) AS n FROM ti_learning_shadows GROUP BY status"
            ).fetchall()
            used = c.execute(
                "SELECT COUNT(*) FROM ti_learning_shadows WHERE budget_day=? AND reserved=1", (day,)
            ).fetchone()[0]
            completed = c.execute(
                "SELECT payload FROM ti_learning_shadows WHERE status='COMPLETED'"
            ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        valid = sum(json.loads(r["payload"]).get("format_valid", False) for r in completed)
        n = counts.get("COMPLETED", 0)
        return {
            "counts": counts,
            "completed": n,
            "format_valid_rate": valid / n if n else None,
            "budget_day": day,
            "daily_used": used,
            "daily_limit": 10,
            "sample_rate": 0.1,
            "evidence_status": "INSUFFICIENT_SAMPLES" if n < 30 else "OBSERVATIONAL_ONLY",
            "cost": None,
            "cost_status": "UNKNOWN",
            "automatic_promotion": False,
        }

    def recover(self):
        with self.store._lock, self.store.connect() as c:
            c.execute(
                "UPDATE ti_learning_shadows SET status='INTERRUPTED' "
                "WHERE status IN ('QUEUED','RUNNING')"
            )
