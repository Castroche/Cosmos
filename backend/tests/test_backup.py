import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from cosmos_quant.backup import BackupService
from cosmos_quant.configuration import UserConfiguration
from cosmos_quant.storage import SqliteStore


def test_online_backup_is_integrity_checked_and_readable(tmp_path):
    store = SqliteStore(tmp_path / "data" / "cosmos.db")
    store.save_configuration(UserConfiguration(default_symbol="ETH/USDT"))
    service = BackupService(store, tmp_path / "backups")

    result = service.create()
    latest = service.latest()

    assert result.integrity == "ok"
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    assert latest is not None
    assert latest.sha256 == result.sha256
    restored = SqliteStore(Path(result.path))
    configuration, _ = restored.load_configuration()
    assert configuration.default_symbol == "ETH/USDT"


def test_backup_allows_trading_state_writes_while_copying(tmp_path, monkeypatch):
    store = SqliteStore(tmp_path / "cosmos.db")
    copying = Event()
    release = Event()

    class PausedBackupConnection(sqlite3.Connection):
        def backup(self, target, **options):
            def progress(*_args):
                copying.set()
                assert release.wait(5)

            return super().backup(target, progress=progress, **options)

    monkeypatch.setattr(
        store,
        "connect",
        lambda: sqlite3.connect(store.path, factory=PausedBackupConnection),
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        backup = workers.submit(store.backup_to, tmp_path / "snapshot.db")
        try:
            assert copying.wait(2)
            write = workers.submit(
                store.save_configuration, UserConfiguration(default_symbol="SOL/USDT")
            )
            write.result(timeout=2)
        finally:
            release.set()
        assert backup.result(timeout=5).is_file()
    restored = SqliteStore(tmp_path / "snapshot.db")
    assert restored.integrity_check() == "ok"


def test_integrity_scan_does_not_wait_for_application_write_lock(tmp_path):
    store = SqliteStore(tmp_path / "cosmos.db")
    with ThreadPoolExecutor(max_workers=1) as workers, store._lock:
        scan = workers.submit(store.integrity_check)
        assert scan.result(timeout=2) == "ok"
