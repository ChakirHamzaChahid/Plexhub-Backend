"""Tests for app.scripts.backup_db."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.scripts import backup_db


def _make_db(path: Path, n_rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO foo (name) VALUES (?)", [(f"row{i}",) for i in range(n_rows)]
        )
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(*) FROM foo").fetchone()[0]
    finally:
        conn.close()


class TestPerformBackup:
    def test_creates_consistent_copy(self, tmp_path):
        src = tmp_path / "live.db"
        _make_db(src, n_rows=5)
        dst = tmp_path / "snap" / "snap.db"
        backup_db.perform_backup(src, dst)
        assert dst.is_file()
        assert _row_count(dst) == 5

    def test_works_under_concurrent_writes(self, tmp_path):
        # Open a writer connection and keep it active during backup —
        # SQLite's online .backup must still succeed.
        src = tmp_path / "live.db"
        _make_db(src, n_rows=2)
        writer = sqlite3.connect(str(src))
        try:
            writer.execute("INSERT INTO foo (name) VALUES ('mid-backup')")
            writer.commit()
            dst = tmp_path / "out.db"
            backup_db.perform_backup(src, dst)
            assert _row_count(dst) == 3
        finally:
            writer.close()


class TestPruneOldBackups:
    def _touch_backup(self, dir: Path, age_days: int) -> Path:
        ts = datetime.now(timezone.utc) - timedelta(days=age_days)
        name = f"plexhub-{ts.strftime('%Y%m%d-%H%M%S')}.db"
        p = dir / name
        p.write_bytes(b"x")
        return p

    def test_deletes_only_old(self, tmp_path):
        recent = self._touch_backup(tmp_path, age_days=1)
        old = self._touch_backup(tmp_path, age_days=10)
        deleted = backup_db.prune_old_backups(tmp_path, retention_days=7)
        assert old in deleted
        assert recent not in deleted
        assert recent.exists()
        assert not old.exists()

    def test_retention_zero_keeps_all(self, tmp_path):
        old = self._touch_backup(tmp_path, age_days=30)
        deleted = backup_db.prune_old_backups(tmp_path, retention_days=0)
        assert deleted == []
        assert old.exists()

    def test_ignores_unrelated_files(self, tmp_path):
        (tmp_path / "random.txt").write_text("x")
        (tmp_path / "plexhub-bogus.db").write_text("x")  # bad timestamp format
        old = self._touch_backup(tmp_path, age_days=10)
        deleted = backup_db.prune_old_backups(tmp_path, retention_days=7)
        assert deleted == [old]
        assert (tmp_path / "random.txt").exists()
        assert (tmp_path / "plexhub-bogus.db").exists()


class TestRunBackupIntegration:
    def test_end_to_end(self, tmp_path, monkeypatch):
        # Redirect settings to a tmp DB + tmp backup dir.
        monkeypatch.setattr(backup_db.settings, "DB_PATH", tmp_path / "live.db")
        monkeypatch.setattr(backup_db.settings, "BACKUP_DIR", tmp_path / "backups")
        monkeypatch.setattr(backup_db.settings, "BACKUP_RETENTION_DAYS", 7)
        _make_db(backup_db.settings.DB_PATH, n_rows=2)

        target = backup_db.run_backup()
        assert target.is_file()
        assert target.parent == backup_db.settings.BACKUP_DIR
        assert _row_count(target) == 2
