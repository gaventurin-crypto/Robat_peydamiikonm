from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _reserve_destination(path: str | Path, source: Path) -> Path:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()
    if resolved == source.resolve():
        raise ValueError("Backup source and destination must be different")
    descriptor = os.open(resolved, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    return resolved


def _check_integrity(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise sqlite3.DatabaseError("SQLite integrity check failed")


def _copy_database(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = _reserve_destination(destination, source_path)
    source_connection = target_connection = None
    try:
        source_connection = sqlite3.connect(source_path, timeout=30)
        _check_integrity(source_connection)
        target_connection = sqlite3.connect(destination_path, timeout=30)
        source_connection.backup(target_connection)
        _check_integrity(target_connection)
        target_connection.close()
        target_connection = None
        os.chmod(destination_path, 0o600)
        return destination_path
    except Exception:
        if target_connection is not None:
            target_connection.close()
        destination_path.unlink(missing_ok=True)
        raise
    finally:
        if source_connection is not None:
            source_connection.close()


def create_backup(database_path: str | Path, backup_path: str | Path) -> Path:
    """Create a consistent SQLite snapshot, including committed WAL data."""
    return _copy_database(database_path, backup_path)


def restore_backup(backup_path: str | Path, new_database_path: str | Path) -> Path:
    """Restore a snapshot to a new file without overwriting an existing database."""
    return _copy_database(backup_path, new_database_path)