from pathlib import Path

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
