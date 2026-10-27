import json
import sqlite3
from contextlib import closing
from pathlib import Path

from cosmos_quant.cleanup_v2 import (
    CleanupDisposition,
    build_cleanup_manifest,
    compact_database,
    create_verified_backup,
    delete_prunable_datasets,
    prune_legacy_backups,
    replace_database,
    verify_database,
)
from cosmos_quant.storage import SqliteStore


def _dataset(dataset_id: str, *, symbol: str, source_dataset_id: str | None = None) -> dict:
    return {
        "id": dataset_id,
        "data_fingerprint": f"fingerprint-{dataset_id}",
        "exchange": "okx",
        "market_type": "swap",
        "symbol": symbol,
        "timeframe": "5m",
        "research_role": "general",
        "source_dataset_id": source_dataset_id,
        "created_at": "2026-08-01T00:00:00+00:00",
    }


def _insert_dataset(database: Path, payload: dict) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO research_datasets
                (dataset_id, data_fingerprint, exchange_id, market_type,
                 symbol, timeframe, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["data_fingerprint"],
                payload["exchange"],
                payload["market_type"],
                payload["symbol"],
                payload["timeframe"],
                json.dumps(payload),
                payload["created_at"],
            ),
        )
        connection.commit()


def test_cleanup_manifest_preserves_archives_and_referenced_datasets(tmp_path):
    workspace = tmp_path
    database = workspace / "data" / "cosmos.db"
    SqliteStore(database)
    archives = workspace / "data" / "archives"
    archives.mkdir(parents=True)
    (archives / "raw.zip").write_bytes(b"official")
    (workspace / "output" / "acceptance").mkdir(parents=True)

    retained = _dataset("ds_11111111", symbol="BTC/USDT")
    prunable = _dataset("ds_22222222", symbol="DOGE/USDT")
    _insert_dataset(database, retained)
    _insert_dataset(database, prunable)
    (workspace / "output" / "acceptance" / "latest.json").write_text(
        json.dumps({"dataset_id": retained["id"]}),
        encoding="utf-8",
    )

    manifest = build_cleanup_manifest(workspace, database)

    assert manifest["dataset_retention"]["retained_ids"] == [retained["id"]]
    assert manifest["dataset_retention"]["prunable_ids"] == [prunable["id"]]
    archive_item = next(
        item for item in manifest["items"] if item["path"].endswith("archives")
    )
    assert archive_item["disposition"] == CleanupDisposition.KEEP
    database_item = next(
        item for item in manifest["items"] if item["path"].endswith("cosmos.db")
    )
    assert database_item["disposition"] == CleanupDisposition.KEEP
    assert database_item["database_record_count"] == 2


def test_dataset_prune_is_transactional_and_idempotent(tmp_path):
    database = tmp_path / "data" / "cosmos.db"
    SqliteStore(database)
    payload = _dataset("ds_33333333", symbol="DOGE/USDT")
    _insert_dataset(database, payload)

    first = delete_prunable_datasets(database, [payload["id"]])
    second = delete_prunable_datasets(database, [payload["id"]])

    assert first["research_datasets"] == 1
    assert second["research_datasets"] == 0


def test_backup_prune_and_compact_are_verified(tmp_path):
    database = tmp_path / "data" / "cosmos.db"
    SqliteStore(database)
    payload = _dataset("ds_44444444", symbol="BTC/USDT")
    _insert_dataset(database, payload)
    backups = tmp_path / "data" / "backups"
    retained = backups / "cosmos-pre-v2-cleanup-test.db"
    old = backups / "cosmos-old.db"

    backup = create_verified_backup(database, retained)
    old.write_bytes(retained.read_bytes())
    deleted = prune_legacy_backups(backups, keep=[retained])
    compacted = tmp_path / "data" / "compacted.db"
    compact_database(database, compacted)
    replaced = replace_database(database, compacted)

    assert backup["integrity"] == "ok"
    assert str(old.resolve()) in deleted
    assert retained.exists()
    assert not old.exists()
    assert replaced["integrity"] == "ok"
    assert verify_database(database)["integrity"] == "ok"
