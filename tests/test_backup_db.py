"""Tests for app.scripts.backup_db."""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Lot 10 sous-lot A — FR-BE-875 (integrity) / FR-BE-876 (atomic write)
# ---------------------------------------------------------------------------


def _make_wal_db(path: Path, n_rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO foo (name) VALUES (?)", [(f"row{i}",) for i in range(n_rows)]
        )
        conn.commit()
    finally:
        conn.close()


def _residuals(directory: Path) -> list[str]:
    """Anything in `directory` that is a backup file or a leftover temp file."""
    return sorted(
        p.name for p in directory.iterdir()
        if p.name.startswith("plexhub-") or p.name.endswith((".tmp", "-wal", "-shm", "-journal"))
    )


class TestAtomicWrite:
    def test_no_tmp_left_after_success(self, tmp_path):
        src = tmp_path / "live.db"
        _make_db(src)
        out = tmp_path / "out"
        dst = out / "plexhub-x.db"
        backup_db.perform_backup(src, dst)
        assert [p.name for p in out.iterdir()] == ["plexhub-x.db"]

    def test_failure_mid_copy_leaves_nothing(self, tmp_path, monkeypatch):
        src = tmp_path / "live.db"
        _make_db(src)
        out = tmp_path / "out"
        dst = out / "plexhub-20990101-000000-000001.db"

        def _boom(source, dest):
            Path(dest).write_bytes(b"partial-garbage")  # partial copy on disk
            raise OSError("disk full")

        monkeypatch.setattr(backup_db, "snapshot_copy", _boom)
        with pytest.raises(OSError, match="disk full"):
            backup_db.perform_backup(src, dst)
        assert _residuals(out) == []

    def test_interruption_leaves_nothing(self, tmp_path, monkeypatch):
        src = tmp_path / "live.db"
        _make_db(src)
        out = tmp_path / "out"
        dst = out / "plexhub-20990101-000000-000001.db"

        def _interrupt(source, dest):
            Path(dest).write_bytes(b"partial")
            raise KeyboardInterrupt

        monkeypatch.setattr(backup_db, "snapshot_copy", _interrupt)
        with pytest.raises(KeyboardInterrupt):
            backup_db.perform_backup(src, dst)
        assert _residuals(out) == []

    def test_final_name_appears_only_after_check(self, tmp_path, monkeypatch):
        src = tmp_path / "live.db"
        _make_db(src)
        dst = tmp_path / "out" / "plexhub-x.db"
        seen: dict[str, bool] = {}

        real_check = backup_db.check_integrity

        def _spy(path):
            seen["final_exists_during_check"] = dst.exists()
            seen["tmp_exists_during_check"] = path.exists() and path.name.endswith(".tmp")
            real_check(path)

        monkeypatch.setattr(backup_db, "check_integrity", _spy)
        backup_db.perform_backup(src, dst)
        assert seen == {"final_exists_during_check": False, "tmp_exists_during_check": True}
        assert dst.is_file()

    def test_backup_of_wal_db_is_single_self_contained_file(self, tmp_path):
        src = tmp_path / "live.db"
        _make_wal_db(src, n_rows=4)
        out = tmp_path / "out"
        dst = out / "plexhub-x.db"
        backup_db.perform_backup(src, dst)
        assert _row_count(dst) == 4
        assert [p.name for p in out.iterdir()] == ["plexhub-x.db"]  # no -wal/-shm sidecars

    def test_target_name_has_microseconds_and_never_collides(self):
        now = datetime(2026, 10, 2, 4, 0, 0, 123456, tzinfo=timezone.utc)
        a = backup_db._build_target_path(Path("d"), now)
        b = backup_db._build_target_path(Path("d"), now.replace(microsecond=123457))
        assert a.name == "plexhub-20261002-040000-123456.db"
        assert a != b


class TestIntegrityCheck:
    def test_check_integrity_ok(self, tmp_path):
        db = tmp_path / "ok.db"
        _make_db(db)
        backup_db.check_integrity(db)  # no exception

    def test_check_integrity_rejects_non_database(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"this is not a sqlite file" * 100)
        with pytest.raises(backup_db.BackupIntegrityError):
            backup_db.check_integrity(bad)

    def test_check_integrity_rejects_corrupted_pages(self, tmp_path):
        db = tmp_path / "corrupt.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO foo (name) VALUES (?)", [("x" * 200,)] * 500)
        conn.commit()
        conn.close()
        raw = bytearray(db.read_bytes())
        for off in range(4096 * 3, 4096 * 3 + 600):  # smash a b-tree leaf page
            raw[off] = 0xFF
        db.write_bytes(bytes(raw))
        with pytest.raises(backup_db.BackupIntegrityError):
            backup_db.check_integrity(db)

    def test_failed_check_rejects_copy_and_leaves_no_file(self, tmp_path, monkeypatch):
        src = tmp_path / "live.db"
        _make_db(src)
        out = tmp_path / "out"
        dst = out / "plexhub-20990101-000000-000001.db"

        def _ko(path):
            raise backup_db.BackupIntegrityError("integrity_check failed on x: *** in database main ***")

        monkeypatch.setattr(backup_db, "check_integrity", _ko)
        with pytest.raises(backup_db.BackupIntegrityError):
            backup_db.perform_backup(src, dst)
        assert _residuals(out) == []

    def test_failed_check_does_not_prune_old_backups(self, tmp_path, monkeypatch):
        live = tmp_path / "live.db"
        _make_db(live)
        backups = tmp_path / "backups"
        backups.mkdir()
        old = backups / f"plexhub-{(datetime.now(timezone.utc) - timedelta(days=60)).strftime('%Y%m%d-%H%M%S')}.db"
        old.write_bytes(b"old-but-precious")
        monkeypatch.setattr(backup_db.settings, "DB_PATH", live)
        monkeypatch.setattr(backup_db.settings, "BACKUP_DIR", backups)
        monkeypatch.setattr(backup_db.settings, "BACKUP_RETENTION_DAYS", 7)

        def _ko(path):
            raise backup_db.BackupIntegrityError("nope")

        monkeypatch.setattr(backup_db, "check_integrity", _ko)
        with pytest.raises(backup_db.BackupIntegrityError):
            backup_db.run_backup()
        assert old.exists()
        assert _residuals(backups) == [old.name]

    def test_integrity_result_is_logged(self, tmp_path):
        src = tmp_path / "live.db"
        _make_db(src)
        logger = logging.getLogger("plexhub.scripts.backup")
        records: list[str] = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        h = _H(level=logging.INFO)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(h)
        try:
            backup_db.perform_backup(src, tmp_path / "out" / "plexhub-x.db")
        finally:
            logger.removeHandler(h)
            logger.setLevel(old_level)
        assert any("integrity_check: ok" in m for m in records)

    def test_vec0_virtual_table_passes_without_extension(self, tmp_path):
        sqlite_vec = pytest.importorskip("sqlite_vec")
        src = tmp_path / "vec.db"
        conn = sqlite3.connect(str(src))
        try:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except (AttributeError, sqlite3.OperationalError) as exc:
                pytest.skip(f"sqlite-vec not loadable here: {exc}")
            conn.execute("CREATE VIRTUAL TABLE vtest USING vec0(embedding FLOAT[4])")
            conn.execute(
                "INSERT INTO vtest(rowid, embedding) VALUES (1, ?)",
                (sqlite_vec.serialize_float32([0.1, 0.2, 0.3, 0.4]),),
            )
            conn.commit()
        finally:
            conn.close()
        # perform_backup's copy + integrity check use a PLAIN connection (no vec).
        dst = tmp_path / "out" / "plexhub-x.db"
        backup_db.perform_backup(src, dst)
        assert dst.is_file()


class TestOrphanTmpSweep:
    def test_old_orphan_swept_recent_kept(self, tmp_path):
        old = tmp_path / "plexhub-20250101-000000-000001.db.tmp"
        recent = tmp_path / "plexhub-20990101-000000-000001.db.tmp"
        other = tmp_path / "unrelated.tmp"
        for p in (old, recent, other):
            p.write_bytes(b"x")
        two_h_ago = time.time() - 7200
        os.utime(old, (two_h_ago, two_h_ago))
        os.utime(other, (two_h_ago, two_h_ago))
        deleted = backup_db.sweep_orphan_tmp(tmp_path)
        assert deleted == [old]
        assert not old.exists()
        assert recent.exists()
        assert other.exists()  # not ours

    def test_sweep_never_touches_real_backups(self, tmp_path):
        real = tmp_path / "plexhub-20250101-000000.db"
        real.write_bytes(b"x")
        two_h_ago = time.time() - 7200
        os.utime(real, (two_h_ago, two_h_ago))
        assert backup_db.sweep_orphan_tmp(tmp_path) == []
        assert real.exists()

    def test_sweep_missing_dir_is_noop(self, tmp_path):
        assert backup_db.sweep_orphan_tmp(tmp_path / "nope") == []

    def test_run_backup_sweeps_orphans_first(self, tmp_path, monkeypatch):
        live = tmp_path / "live.db"
        _make_db(live)
        backups = tmp_path / "backups"
        backups.mkdir()
        orphan = backups / "plexhub-20250101-000000-000001.db.tmp"
        orphan.write_bytes(b"x")
        two_h_ago = time.time() - 7200
        os.utime(orphan, (two_h_ago, two_h_ago))
        monkeypatch.setattr(backup_db.settings, "DB_PATH", live)
        monkeypatch.setattr(backup_db.settings, "BACKUP_DIR", backups)
        monkeypatch.setattr(backup_db.settings, "BACKUP_RETENTION_DAYS", 7)
        target = backup_db.run_backup()
        assert not orphan.exists()
        assert target.is_file()


class TestPruneBothNameFormats:
    @staticmethod
    def _make(dir: Path, age_days: int, micro: bool) -> Path:
        ts = datetime.now(timezone.utc) - timedelta(days=age_days)
        fmt = "%Y%m%d-%H%M%S-%f" if micro else "%Y%m%d-%H%M%S"
        p = dir / f"plexhub-{ts.strftime(fmt)}.db"
        p.write_bytes(b"x")
        return p

    def test_legacy_and_microsecond_names_are_pruned_alike(self, tmp_path):
        old_legacy = self._make(tmp_path, 10, micro=False)
        old_micro = self._make(tmp_path, 10, micro=True)
        new_legacy = self._make(tmp_path, 1, micro=False)
        new_micro = self._make(tmp_path, 1, micro=True)
        deleted = backup_db.prune_old_backups(tmp_path, retention_days=7)
        assert sorted(deleted) == sorted([old_legacy, old_micro])
        assert new_legacy.exists() and new_micro.exists()

    def test_prune_ignores_tmp_and_prerestore_files(self, tmp_path):
        keep = [
            tmp_path / "plexhub-20200101-000000-000001.db.tmp",
            tmp_path / "plexhub.pre-restore-20200101-000000-000001.db",
            tmp_path / "plexhub-20200101-000000-bad.db",
        ]
        for p in keep:
            p.write_bytes(b"x")
        assert backup_db.prune_old_backups(tmp_path, retention_days=7) == []
        assert all(p.exists() for p in keep)


class TestBackupUnderRealWalWriter:
    """A REAL concurrent writer on a WAL database (model: test_db_retry_real_lock.py)."""

    def test_backup_stays_consistent_while_writer_commits(self, tmp_path):
        src = tmp_path / "live.db"
        _make_wal_db(src, n_rows=50)

        stop = threading.Event()
        started = threading.Event()
        written = {"n": 0}

        def _writer() -> None:
            conn = sqlite3.connect(str(src), timeout=30)
            try:
                started.set()
                while not stop.is_set():
                    conn.execute("INSERT INTO foo (name) VALUES ('live')")
                    conn.commit()
                    written["n"] += 1
            finally:
                conn.close()

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        assert started.wait(timeout=5)
        try:
            dst = tmp_path / "out" / "plexhub-x.db"
            backup_db.perform_backup(src, dst)
        finally:
            stop.set()
            t.join(timeout=10)
        assert not t.is_alive()
        assert written["n"] > 0

        backup_db.check_integrity(dst)
        conn = sqlite3.connect(str(dst))
        try:
            n, lo, hi = conn.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM foo").fetchone()
        finally:
            conn.close()
        # Snapshot is a consistent prefix: ids are contiguous 1..N (no torn/partial state).
        assert n >= 50
        assert (lo, hi) == (1, n)
