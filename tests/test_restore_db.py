"""Tests for app.scripts.restore_db (Lot 10 sous-lot A, FR-BE-874).

Every test works on tmp_path only — the real data/plexhub.db is never touched.
`fcntl` does not exist on Windows: the lock is isolated in
`restore_db._acquire_master_lock`, monkeypatched here (plus one real-flock test
skipped where fcntl is unavailable).
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.scripts import restore_db


def _make_db(path: Path, rows: list[str], wal: bool = False) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO foo (name) VALUES (?)", [(r,) for r in rows])
        conn.commit()
    finally:
        conn.close()


def _names(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT name FROM foo ORDER BY id")]
    finally:
        conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """data/ with a live DB (WAL leftovers included) + a good backup elsewhere."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "plexhub.db"
    _make_db(db, ["current-1", "current-2"])
    # Simulate a crashed app: stale WAL/SHM next to the live DB.
    (data / "plexhub.db-wal").write_bytes(b"stale-wal")
    (data / "plexhub.db-shm").write_bytes(b"stale-shm")

    backups = tmp_path / "backups"
    backups.mkdir()
    good = backups / "plexhub-20260101-000000-000001.db"
    _make_db(good, ["backup-1", "backup-2", "backup-3"])

    # The app is "stopped": lock acquirable (no-op handle).
    monkeypatch.setattr(restore_db, "_acquire_master_lock", lambda lock_path: None)
    return {"data": data, "db": db, "good": good, "backups": backups}


class TestDryRun:
    def test_dry_run_changes_nothing(self, env):
        before = env["db"].read_bytes()
        names_before = sorted(p.name for p in env["data"].iterdir())
        lines = restore_db.restore(env["good"], env["db"], apply=False)
        assert env["db"].read_bytes() == before
        assert sorted(p.name for p in env["data"].iterdir()) == names_before
        assert any("DRY-RUN" in line for line in lines)
        assert any("integrity_check ok" in line for line in lines)

    def test_main_defaults_to_dry_run(self, env, monkeypatch, capsys):
        monkeypatch.setattr(restore_db.settings, "DB_PATH", env["db"])
        rc = restore_db.main([str(env["good"])])
        assert rc == 0
        assert _names(env["db"]) == ["current-1", "current-2"]
        assert "DRY-RUN" in capsys.readouterr().out


class TestApply:
    def test_apply_replaces_database(self, env):
        restore_db.restore(env["good"], env["db"], apply=True)
        assert _names(env["db"]) == ["backup-1", "backup-2", "backup-3"]

    def test_current_db_saved_before_replacement(self, env):
        restore_db.restore(env["good"], env["db"], apply=True)
        saved = [p for p in env["data"].iterdir() if p.name.startswith("plexhub.pre-restore-")]
        assert len(saved) == 1
        assert saved[0].name.endswith(".db")
        assert _names(saved[0]) == ["current-1", "current-2"]
        # Outside the automatic pruning pattern ("plexhub-*.db").
        assert not saved[0].name.startswith("plexhub-")

    def test_stale_wal_and_shm_removed(self, env):
        restore_db.restore(env["good"], env["db"], apply=True)
        assert not (env["data"] / "plexhub.db-wal").exists()
        assert not (env["data"] / "plexhub.db-shm").exists()
        # And the restored DB reads cleanly (a stale WAL would break this).
        assert _names(env["db"]) == ["backup-1", "backup-2", "backup-3"]

    def test_real_wal_current_db_is_captured_in_safety_copy(self, tmp_path, monkeypatch):
        """Rows living only in the live WAL (crashed app) must survive in plexhub.pre-restore-*."""
        monkeypatch.setattr(restore_db, "_acquire_master_lock", lambda lock_path: None)
        live = tmp_path / "live"
        live.mkdir()
        _make_db(live / "plexhub.db", ["wal-1"], wal=True)
        holder = sqlite3.connect(str(live / "plexhub.db"))
        holder.execute("INSERT INTO foo (name) VALUES ('wal-2')")
        holder.commit()
        # Freeze the on-disk state of a "crashed" app: .db + un-checkpointed -wal, no process.
        data = tmp_path / "data"
        data.mkdir()
        db = data / "plexhub.db"
        shutil.copy(live / "plexhub.db", db)
        shutil.copy(live / "plexhub.db-wal", data / "plexhub.db-wal")
        holder.close()
        assert (data / "plexhub.db-wal").stat().st_size > 0

        good = tmp_path / "good.db"
        _make_db(good, ["b"])
        restore_db.restore(good, db, apply=True)

        saved = next(p for p in data.iterdir() if p.name.startswith("plexhub.pre-restore-"))
        assert _names(saved) == ["wal-1", "wal-2"]
        assert _names(db) == ["b"]
        assert not (data / "plexhub.db-wal").exists()

    def test_no_residual_tmp_after_apply(self, env):
        restore_db.restore(env["good"], env["db"], apply=True)
        assert not [p for p in env["data"].iterdir() if p.name.endswith((".tmp", ".restore-tmp"))]

    def test_restore_without_current_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(restore_db, "_acquire_master_lock", lambda lock_path: None)
        data = tmp_path / "data"
        data.mkdir()
        good = tmp_path / "good.db"
        _make_db(good, ["a", "b"])
        db = data / "plexhub.db"
        restore_db.restore(good, db, apply=True)
        assert _names(db) == ["a", "b"]
        assert not [p for p in data.iterdir() if "pre-restore" in p.name]

    def test_main_apply_success_exit_zero(self, env, monkeypatch):
        monkeypatch.setattr(restore_db.settings, "DB_PATH", env["db"])
        assert restore_db.main([str(env["good"]), "--apply"]) == 0
        assert _names(env["db"]) == ["backup-1", "backup-2", "backup-3"]


class TestRefusals:
    def test_refuses_when_lock_held(self, env, monkeypatch):
        def _held(lock_path):
            raise restore_db.RestoreError("L'application tient le verrou master")

        monkeypatch.setattr(restore_db, "_acquire_master_lock", _held)
        before = env["db"].read_bytes()
        with pytest.raises(restore_db.RestoreError, match="verrou master"):
            restore_db.restore(env["good"], env["db"], apply=True)
        assert env["db"].read_bytes() == before
        assert (env["data"] / "plexhub.db-wal").exists()  # nothing deleted
        assert not [p for p in env["data"].iterdir() if "pre-restore" in p.name]

    def test_dry_run_also_reports_held_lock(self, env, monkeypatch):
        def _held(lock_path):
            raise restore_db.RestoreError("L'application tient le verrou master")

        monkeypatch.setattr(restore_db, "_acquire_master_lock", _held)
        with pytest.raises(restore_db.RestoreError):
            restore_db.restore(env["good"], env["db"], apply=False)

    def test_unverifiable_lock_refuses_apply_but_not_dry_run(self, env, monkeypatch):
        def _no_fcntl(lock_path):
            raise restore_db.LockUnverifiable("fcntl indisponible")

        monkeypatch.setattr(restore_db, "_acquire_master_lock", _no_fcntl)
        with pytest.raises(restore_db.LockUnverifiable):
            restore_db.restore(env["good"], env["db"], apply=True)
        lines = restore_db.restore(env["good"], env["db"], apply=False)
        assert any("NON VÉRIFIABLE" in line for line in lines)

    def test_refuses_corrupted_source(self, env):
        bad = env["backups"] / "plexhub-20260102-000000-000001.db"
        bad.write_bytes(b"definitely not sqlite" * 200)
        before = env["db"].read_bytes()
        with pytest.raises(restore_db.RestoreError, match="corrompue"):
            restore_db.restore(bad, env["db"], apply=True)
        assert env["db"].read_bytes() == before
        assert (env["data"] / "plexhub.db-wal").exists()
        assert not [p for p in env["data"].iterdir() if "pre-restore" in p.name]

    def test_refuses_missing_source(self, env):
        with pytest.raises(restore_db.RestoreError, match="introuvable"):
            restore_db.restore(env["backups"] / "nope.db", env["db"], apply=True)

    def test_refuses_source_equal_to_current_db(self, env):
        with pytest.raises(restore_db.RestoreError, match="base courante"):
            restore_db.restore(env["db"], env["db"], apply=True)

    def test_main_returns_nonzero_with_message(self, env, monkeypatch, capsys):
        monkeypatch.setattr(restore_db.settings, "DB_PATH", env["db"])
        rc = restore_db.main([str(env["backups"] / "nope.db"), "--apply"])
        assert rc == 1
        assert "ÉCHEC" in capsys.readouterr().err

    def test_failure_during_copy_keeps_current_db_and_no_tmp(self, env, monkeypatch):
        def _boom(source, dest):
            Path(dest).write_bytes(b"partial")
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(restore_db.backup_db, "snapshot_copy", _boom)
        with pytest.raises(restore_db.RestoreError):
            restore_db.restore(env["good"], env["db"], apply=True)
        # The live DB was never replaced; leftovers cleaned.
        assert _names(env["db"]) == ["current-1", "current-2"]
        assert not [p for p in env["data"].iterdir() if p.name.endswith(".restore-tmp")]


class TestCorruptCurrentDb:
    def test_corrupt_current_db_does_not_block_restore(self, env):
        env["db"].write_bytes(b"corrupt current database" * 300)
        restore_db.restore(env["good"], env["db"], apply=True)
        assert _names(env["db"]) == ["backup-1", "backup-2", "backup-3"]
        saved = [p for p in env["data"].iterdir() if p.name.startswith("plexhub.pre-restore-")]
        assert len(saved) == 1  # raw-copy fallback preserved the damaged file


class TestRealMasterLock:
    def test_real_flock_held_is_refused(self, tmp_path):
        fcntl = pytest.importorskip("fcntl")
        lock = tmp_path / "server_start.lock"
        holder = open(lock, "w")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(restore_db.RestoreError, match="verrou master"):
                restore_db._acquire_master_lock(lock)
        finally:
            holder.close()
        handle = restore_db._acquire_master_lock(lock)  # free again
        restore_db._release_lock(handle)

    def test_missing_fcntl_degrades_with_clear_message(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def _no_fcntl(name, *a, **k):
            if name == "fcntl":
                raise ImportError("no fcntl")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _no_fcntl)
        with pytest.raises(restore_db.LockUnverifiable, match="fcntl indisponible"):
            restore_db._acquire_master_lock(tmp_path / "server_start.lock")


class TestRawCopyFallbackAtomic:
    def test_failed_raw_copy_leaves_no_prerestore_file(self, env, monkeypatch):
        """A raw-copy failure must not leave a truncated plexhub.pre-restore-*.db."""
        def _snapshot_fails(*a, **kw):
            raise sqlite3.DatabaseError("file is not a database")

        def _copy_fails(src, dst, *a, **kw):
            Path(dst).write_bytes(b"partial")  # truncated copy on disk
            raise OSError("disk full")

        monkeypatch.setattr(restore_db.backup_db, "perform_backup", _snapshot_fails)
        monkeypatch.setattr(restore_db.shutil, "copy2", _copy_fails)
        with pytest.raises(OSError, match="disk full"):
            restore_db._save_current_db(env["db"])
        assert not [p for p in env["data"].iterdir() if "pre-restore" in p.name]

    def test_raw_copy_success_publishes_final_name_only(self, env, monkeypatch):
        def _snapshot_fails(*a, **kw):
            raise sqlite3.DatabaseError("file is not a database")

        monkeypatch.setattr(restore_db.backup_db, "perform_backup", _snapshot_fails)
        saved = restore_db._save_current_db(env["db"])
        assert saved.is_file()
        assert saved.read_bytes() == env["db"].read_bytes()
        assert not [p for p in env["data"].iterdir() if p.name.endswith(".tmp")]

    def test_restore_logs_integrity_duration(self, env):
        import logging

        records: list[logging.LogRecord] = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)

        log = logging.getLogger("plexhub.scripts.restore")
        h, old = _H(level=logging.INFO), log.level
        log.setLevel(logging.INFO)
        log.addHandler(h)
        try:
            restore_db.restore(env["good"], env["db"], apply=True)
        finally:
            log.removeHandler(h)
            log.setLevel(old)
        durations = [r for r in records if "integrity_check duration" in r.getMessage()]
        assert len(durations) == 2  # source + verified copy
