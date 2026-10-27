from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel

from .storage import SqliteStore


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Hash large database backups without loading them into memory."""

    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class BackupResult(BaseModel):
    path: str
    created_at: datetime
    size_bytes: int
    sha256: str
    integrity: str


class BackupService:
    def __init__(
        self,
        store: SqliteStore,
        directory: Path,
        retention_count: int = 30,
    ):
        self.store = store
        self.directory = directory
        self.retention_count = retention_count

    def create(self) -> BackupResult:
        now = datetime.now(UTC)
        # Microseconds prevent two operator-triggered backups in the same second
        # from overwriting one another.
        path = self.directory / f"cosmos-{now.strftime('%Y%m%dT%H%M%S%fZ')}.db"
        self.store.backup_to(path)
        digest = sha256_file(path)
        self._prune()
        return BackupResult(
            path=str(path.resolve()),
            created_at=now,
            size_bytes=path.stat().st_size,
            sha256=digest,
            integrity="ok",
        )

    def create_if_due(self, interval: timedelta = timedelta(hours=24)) -> BackupResult | None:
        backups = self._backups()
        if backups:
            modified = datetime.fromtimestamp(backups[0].stat().st_mtime, tz=UTC)
            if datetime.now(UTC) - modified < interval:
                return None
        return self.create()

    def latest(self) -> BackupResult | None:
        backups = self._backups()
        if not backups:
            return None
        path = backups[0]
        return BackupResult(
            path=str(path.resolve()),
            created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
            integrity=self._verify(path),
        )

    def _verify(self, path: Path) -> str:
        import sqlite3

        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "no result"
        finally:
            connection.close()

    def _backups(self) -> list[Path]:
        self.directory.mkdir(parents=True, exist_ok=True)
        return sorted(
            self.directory.glob("cosmos-*.db"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _prune(self) -> None:
        for expired in self._backups()[self.retention_count :]:
            expired.unlink()
