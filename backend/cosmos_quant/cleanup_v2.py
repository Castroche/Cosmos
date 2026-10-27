from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .backup import sha256_file

V2_SYMBOLS = frozenset({"BTC/USDT", "ETH/USDT", "SOL/USDT"})
V2_EXCHANGES = frozenset({"okx", "binance"})
DATASET_ID = re.compile(r"\bds_[0-9a-f]{8,}\b")
RESEARCH_CHILD_TABLES = (
    "research_dataset_liquidation_flow",
    "research_dataset_order_flow",
    "research_dataset_basis",
    "research_dataset_positioning",
    "research_dataset_open_interest",
    "research_dataset_funding",
    "research_dataset_candles",
)
REBUILD_PATHS = (
    ".venv",
    "node_modules",
    "apps/web/node_modules",
    "apps/web/dist",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "backend/cosmos_quant.egg-info",
    "backend/data",
)
OUTPUT_DELETE_PATHS = (
    "output/playwright",
    "output/ui-verification-data",
    "output/runtime",
)


class CleanupDisposition(StrEnum):
    KEEP = "KEEP"
    DELETE = "DELETE"
    REBUILD = "REBUILD"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class CleanupItem:
    path: str
    disposition: CleanupDisposition
    size_bytes: int
    file_count: int
    reason: str
    references: tuple[str, ...] = ()
    database_record_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "disposition": self.disposition.value,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
            "reason": self.reason,
            "references": list(self.references),
            "database_record_count": self.database_record_count,
        }


def _safe_workspace_path(workspace: Path, candidate: Path) -> Path:
    root = workspace.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"cleanup target escapes workspace: {resolved}")
    return resolved


def _path_usage(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        return path.stat().st_size, 1
    size = 0
    count = 0
    for item in path.rglob("*"):
        if item.is_file():
            count += 1
            size += item.stat().st_size
    return size, count


def _walk_dataset_ids(value: Any, known: set[str], result: set[str]) -> None:
    if isinstance(value, str):
        if value in known:
            result.add(value)
        return
    if isinstance(value, dict):
        for child in value.values():
            _walk_dataset_ids(child, known, result)
        return
    if isinstance(value, list):
        for child in value:
            _walk_dataset_ids(child, known, result)


def external_dataset_references(workspace: Path, known: set[str]) -> set[str]:
    references: set[str] = set()
    roots = (workspace / "output" / "acceptance", workspace / "docs")
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".md"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            references.update(item for item in DATASET_ID.findall(text) if item in known)
    return references


def database_dataset_references(
    connection: sqlite3.Connection,
    known: set[str],
) -> set[str]:
    references: set[str] = set()
    storage_tables = {"research_datasets", *RESEARCH_CHILD_TABLES}
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info([{table}])")
        ]
        if table != "research_datasets" and "payload" in columns:
            for (payload,) in connection.execute(
                f"SELECT payload FROM [{table}] WHERE payload IS NOT NULL"
            ):
                try:
                    _walk_dataset_ids(json.loads(payload), known, references)
                except (TypeError, json.JSONDecodeError):
                    continue
        if table in storage_tables:
            continue
        for column in columns:
            if "dataset_id" not in column.lower():
                continue
            for (value,) in connection.execute(
                f"SELECT [{column}] FROM [{table}] WHERE [{column}] IS NOT NULL"
            ):
                if value in known:
                    references.add(str(value))
    return references


def dataset_retention(
    database: Path,
    workspace: Path,
) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT dataset_id, payload FROM research_datasets"
        ).fetchall()
        payloads = {str(dataset_id): json.loads(payload) for dataset_id, payload in rows}
        known = set(payloads)
        database_refs = database_dataset_references(connection, known)
        external_refs = external_dataset_references(workspace, known)
        retained = database_refs | external_refs

        # Preserve the latest general dataset for every v2 market/timeframe even
        # before it is referenced by a campaign or model.
        latest: dict[tuple[str, str, str], tuple[datetime, str]] = {}
        for dataset_id, payload in payloads.items():
            if (
                str(payload.get("exchange", "")).lower() not in V2_EXCHANGES
                or payload.get("symbol") not in V2_SYMBOLS
                or payload.get("market_type") != "swap"
                or payload.get("research_role") not in {None, "general"}
            ):
                continue
            raw_created = str(payload.get("created_at") or "1970-01-01T00:00:00+00:00")
            created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            key = (
                str(payload.get("exchange")),
                str(payload.get("symbol")),
                str(payload.get("timeframe")),
            )
            if key not in latest or created > latest[key][0]:
                latest[key] = (created, dataset_id)
        retained.update(item[1] for item in latest.values())

        # A retained split keeps its immutable source lineage.
        changed = True
        while changed:
            before = len(retained)
            for dataset_id in tuple(retained):
                parent = payloads.get(dataset_id, {}).get("source_dataset_id")
                if parent in known:
                    retained.add(str(parent))
            changed = len(retained) != before

        prunable = known - retained
        child_rows: dict[str, int] = {}
        if prunable:
            placeholders = ",".join("?" for _ in prunable)
            arguments = tuple(sorted(prunable))
            for table in RESEARCH_CHILD_TABLES:
                child_rows[table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM [{table}] "
                        f"WHERE dataset_id IN ({placeholders})",
                        arguments,
                    ).fetchone()[0]
                )
        return {
            "known_count": len(known),
            "database_reference_count": len(database_refs),
            "external_reference_count": len(external_refs),
            "retained_count": len(retained),
            "prunable_count": len(prunable),
            "retained_ids": sorted(retained),
            "prunable_ids": sorted(prunable),
            "prunable_child_rows": child_rows,
        }
    finally:
        connection.close()


def build_cleanup_manifest(workspace: Path, database: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    database = _safe_workspace_path(workspace, database)
    items: list[CleanupItem] = []
    retention = dataset_retention(database, workspace)

    database_size, database_count = _path_usage(database)
    items.append(
        CleanupItem(
            path=str(database),
            disposition=CleanupDisposition.KEEP,
            size_bytes=database_size,
            file_count=database_count,
            reason="active schema-v10 database for v2 runtime and retained audit lineage",
            references=("Trading Intelligence v2", "Decision Ledger", "research lineage"),
            database_record_count=retention["known_count"],
        )
    )

    archives = _safe_workspace_path(workspace, workspace / "data" / "archives")
    archive_size, archive_count = _path_usage(archives)
    items.append(
        CleanupItem(
            path=str(archives),
            disposition=CleanupDisposition.KEEP,
            size_bytes=archive_size,
            file_count=archive_count,
            reason="operator requested all raw market archives be retained",
            references=("v2 research", "offline reproducibility"),
        )
    )

    model_root = _safe_workspace_path(
        workspace, workspace / "data" / "models" / "quant-intelligence-v1"
    )
    model_size, model_count = _path_usage(model_root)
    if model_count:
        items.append(
            CleanupItem(
                path=str(model_root),
                disposition=CleanupDisposition.KEEP,
                size_bytes=model_size,
                file_count=model_count,
                reason="trained BTC/ETH/SOL Quant Intelligence v1 artifacts",
                references=("Quant v1", "source dataset lineage", "live quant gate"),
                database_record_count=model_count,
            )
        )

    for relative, reason, references in (
        (
            "output/cleanup-v2",
            "latest cleanup manifest and destructive-operation evidence",
            ("cleanup acceptance",),
        ),
        (
            "output/upgrade-v2",
            "latest v2 upgrade and verification evidence",
            ("v2 acceptance",),
        ),
    ):
        evidence = _safe_workspace_path(workspace, workspace / relative)
        evidence_size, evidence_count = _path_usage(evidence)
        if evidence_count:
            items.append(
                CleanupItem(
                    path=str(evidence),
                    disposition=CleanupDisposition.KEEP,
                    size_bytes=evidence_size,
                    file_count=evidence_count,
                    reason=reason,
                    references=references,
                )
            )

    backup_root = _safe_workspace_path(workspace, workspace / "data" / "backups")
    for path in sorted(backup_root.glob("*")) if backup_root.exists() else []:
        if not path.is_file():
            continue
        disposition = (
            CleanupDisposition.KEEP
            if "pre-v2-cleanup" in path.name or "pre-v2-upgrade" in path.name
            else CleanupDisposition.DELETE
        )
        items.append(
            CleanupItem(
                path=str(path.resolve()),
                disposition=disposition,
                size_bytes=path.stat().st_size,
                file_count=1,
                reason=(
                    "verified v2 recovery point"
                    if disposition == CleanupDisposition.KEEP
                    else "superseded legacy database backup"
                ),
            )
        )

    for relative in REBUILD_PATHS:
        path = _safe_workspace_path(workspace, workspace / relative)
        size, count = _path_usage(path)
        if count:
            items.append(
                CleanupItem(
                    path=str(path),
                    disposition=CleanupDisposition.REBUILD,
                    size_bytes=size,
                    file_count=count,
                    reason="reproducible dependency, cache, or build output",
                )
            )

    for relative in OUTPUT_DELETE_PATHS:
        path = _safe_workspace_path(workspace, workspace / relative)
        size, count = _path_usage(path)
        if count:
            items.append(
                CleanupItem(
                    path=str(path),
                    disposition=CleanupDisposition.DELETE,
                    size_bytes=size,
                    file_count=count,
                    reason="obsolete UI verification artifact or runtime log",
                )
            )
    output_root = _safe_workspace_path(workspace, workspace / "output")
    for pattern in ("*.stdout.log", "*.stderr.log"):
        for path in sorted(output_root.glob(pattern)) if output_root.exists() else []:
            size, count = _path_usage(path)
            items.append(
                CleanupItem(
                    path=str(path.resolve()),
                    disposition=CleanupDisposition.DELETE,
                    size_bytes=size,
                    file_count=count,
                    reason="obsolete service stdout/stderr log",
                )
            )

    return {
        "schema_version": "cosmos-cleanup-v2/1",
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "database": str(database),
        "database_size_bytes": database.stat().st_size,
        "items": [item.as_dict() for item in items],
        "dataset_retention": retention,
        "summary": {
            disposition.value: {
                "size_bytes": sum(
                    item.size_bytes for item in items if item.disposition == disposition
                ),
                "file_count": sum(
                    item.file_count for item in items if item.disposition == disposition
                ),
            }
            for disposition in CleanupDisposition
        },
    }


def write_manifest(manifest: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def verify_database(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        table_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
        return {
            "path": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "integrity": integrity,
            "foreign_key_errors": [list(item) for item in foreign_keys],
            "table_count": table_count,
        }
    finally:
        connection.close()


def create_verified_backup(database: Path, destination: Path) -> dict[str, Any]:
    database = database.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(database, timeout=60)
    target = sqlite3.connect(destination, timeout=60)
    try:
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(target, pages=16_384, sleep=0.05)
        target.commit()
    except Exception:
        target.close()
        source.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
    result = verify_database(destination)
    if result["integrity"] != "ok" or result["foreign_key_errors"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"backup verification failed: {result}")
    return result


def prune_legacy_backups(backup_root: Path, *, keep: Iterable[Path]) -> list[str]:
    root = backup_root.resolve()
    retained = {item.resolve() for item in keep}
    deleted: list[str] = []
    if not root.exists():
        return deleted
    for candidate in sorted(root.iterdir()):
        resolved = candidate.resolve()
        if root not in resolved.parents or resolved in retained:
            continue
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if candidate.suffix.lower() not in {".db", ".db-wal", ".db-shm"}:
            continue
        candidate.unlink()
        deleted.append(str(resolved))
    return deleted


def compact_database(database: Path, destination: Path) -> dict[str, Any]:
    database = database.resolve()
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(destination)
    connection = sqlite3.connect(database, timeout=60)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        escaped = str(destination).replace("'", "''")
        connection.execute(f"VACUUM INTO '{escaped}'")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    result = verify_database(destination)
    if result["integrity"] != "ok" or result["foreign_key_errors"]:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"compacted database verification failed: {result}")
    return result


def replace_database(database: Path, compacted: Path) -> dict[str, Any]:
    database = database.resolve()
    compacted = compacted.resolve()
    source = sqlite3.connect(compacted, timeout=60)
    target = sqlite3.connect(database, timeout=60)
    try:
        source.backup(target, pages=16_384, sleep=0.05)
        target.commit()
    finally:
        target.close()
        source.close()
    result = verify_database(database)
    if result["integrity"] != "ok" or result["foreign_key_errors"]:
        raise RuntimeError(f"replacement database failed verification: {result}")
    compacted.unlink()
    return result


def remove_rebuild_paths(workspace: Path) -> list[dict[str, Any]]:
    workspace = workspace.resolve()
    removed: list[dict[str, Any]] = []
    for relative in REBUILD_PATHS:
        candidate = _safe_workspace_path(workspace, workspace / relative)
        size, file_count = _path_usage(candidate)
        if not candidate.exists():
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed.append(
            {
                "path": str(candidate),
                "size_bytes": size,
                "file_count": file_count,
            }
        )
    for cache in workspace.rglob("__pycache__"):
        resolved = _safe_workspace_path(workspace, cache)
        size, file_count = _path_usage(resolved)
        shutil.rmtree(resolved)
        removed.append(
            {
                "path": str(resolved),
                "size_bytes": size,
                "file_count": file_count,
            }
        )
    return removed


def delete_prunable_datasets(database: Path, prunable_ids: Iterable[str]) -> dict[str, int]:
    identifiers = tuple(sorted(set(prunable_ids)))
    if not identifiers:
        return {table: 0 for table in (*RESEARCH_CHILD_TABLES, "research_datasets")}
    placeholders = ",".join("?" for _ in identifiers)
    deleted: dict[str, int] = {}
    connection = sqlite3.connect(database, timeout=60)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        for table in RESEARCH_CHILD_TABLES:
            cursor = connection.execute(
                f"DELETE FROM [{table}] WHERE dataset_id IN ({placeholders})",
                identifiers,
            )
            deleted[table] = cursor.rowcount
        cursor = connection.execute(
            f"DELETE FROM research_datasets WHERE dataset_id IN ({placeholders})",
            identifiers,
        )
        deleted["research_datasets"] = cursor.rowcount
        connection.commit()
        return deleted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
