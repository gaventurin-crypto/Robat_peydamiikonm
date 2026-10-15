import sqlite3

import pytest

from app.backup import create_backup, restore_backup
from app.database import Database


@pytest.mark.asyncio
async def test_backup_and_restore_preserve_committed_wal_data_privately(tmp_path):
    source = Database(tmp_path / "live.sqlite3")
    await source.initialize()
    case = await source.create_case(
        12345,
        {
            "have": ["آدرس"],
            "need": ["آدرس"],
            "other_request": "synthetic request",
            "description": "synthetic description",
        },
    )
    backup_path = create_backup(source.path, tmp_path / "private" / "snapshot.sqlite3")
    restored_path = restore_backup(backup_path, tmp_path / "recovered.sqlite3")

    restored = Database(restored_path)
    restored_case = await restored.get_case(case["id"])
    assert restored_case["case_number"] == case["case_number"]
    assert restored_case["description"] == "synthetic description"
    assert oct(backup_path.stat().st_mode & 0o777) == "0o600"
    assert oct(restored_path.stat().st_mode & 0o777) == "0o600"
    connection = sqlite3.connect(backup_path)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_backup_and_restore_never_overwrite_existing_files(tmp_path):
    source = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("CREATE TABLE records(value TEXT)")
    finally:
        connection.close()
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"preserve existing file")

    with pytest.raises(FileExistsError):
        create_backup(source, existing)
    with pytest.raises(ValueError):
        create_backup(source, source)

    assert existing.read_bytes() == b"preserve existing file"


def test_backup_includes_committed_data_from_open_wal_database(tmp_path):
    source = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE records(value TEXT)")
        connection.commit()
        connection.execute("INSERT INTO records VALUES ('committed-in-wal')")
        connection.commit()
        assert (tmp_path / "live.sqlite3-wal").exists()

        backup = create_backup(source, tmp_path / "request-bot-backup-wal.sqlite3")

        snapshot = sqlite3.connect(backup)
        try:
            assert snapshot.execute("SELECT value FROM records").fetchone()[0] == "committed-in-wal"
        finally:
            snapshot.close()
    finally:
        connection.close()