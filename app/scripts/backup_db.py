"""Online backup of plexhub.db using SQLite's native backup API.

Runs while the app is live (WAL mode allows concurrent readers/writers).
Output: BACKUP_DIR/plexhub-YYYYMMDD-HHMMSS-ffffff.db, with retention pruning.

Lot 10 sous-lot A (FR-BE-875/876) — a backup is only ever published under its
final name once it is known to be sound:

1. the snapshot is written to ``<final>.tmp`` in the SAME directory (same
   volume, so the closing ``os.replace`` is atomic);
2. ``PRAGMA integrity_check`` runs on that copy (plain ``sqlite3`` connection,
   no sqlite-vec needed — a vec0 table passes the check without the extension);
3. only on ``ok`` is the ``.tmp`` renamed; on ANY failure (including
   KeyboardInterrupt) it is deleted and nothing is pruned.

A ``.tmp`` orphaned by a hard kill (SIGKILL / power loss) is swept by the next
run once it is older than ``_ORPHAN_TMP_MAX_AGE_SECONDS``.

Lot B2 (ADR 0012 D2.3) — the state of the latest backup is readable WITHOUT touching the
database (``GET /api/metadata/v1/backup/status``): before the final rename, a sidecar
``<name>.verified`` records the file's name, size and ``mtime_ns`` together with the
``integrity_check`` result, and ``plexhub-backup-state.json`` records the last attempt.
Neither lives in the database (a restore would make the state regress). A backup made before
this lot has no sidecar: ``--verify-latest`` runs a real check once and writes it.

Usage (manual):
    python -m app.scripts.backup_db [--retention-days N] [--verify-latest]
Restore: see ``app/scripts/restore_db.py`` and ``docs/ops-backup-restore.md``.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger("plexhub.scripts.backup")

_FILE_PREFIX = "plexhub-"
_FILE_SUFFIX = ".db"
_TMP_SUFFIX = ".tmp"
# SQLite sidecars a hard-killed ``.tmp`` copy may have left behind.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
# restore_db writes its verified copy as ``<db name>.restore-tmp`` beside the live DB.
RESTORE_TMP_SUFFIX = ".restore-tmp"
# Legacy name format (second granularity) — still parsed by the pruner so
# backups written before the microsecond suffix existed keep being rotated.
_LEGACY_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"
# Current format: microseconds avoid two backups of the same second colliding.
_TIMESTAMP_FMT = "%Y%m%d-%H%M%S-%f"
# A ``.tmp`` younger than this may belong to a backup still running.
_ORPHAN_TMP_MAX_AGE_SECONDS = 3600
# Lot B2: ``<backup>.db.verified`` records that exact file's integrity_check; the attempt state
# file sits in the same directory and is not a backup (never matched by prune/sweep).
_VERIFIED_SUFFIX = ".verified"
STATE_FILE_NAME = "plexhub-backup-state.json"


class BackupIntegrityError(RuntimeError):
    """The freshly written copy failed ``PRAGMA integrity_check``."""


def _build_target_path(backup_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return backup_dir / f"{_FILE_PREFIX}{now.strftime(_TIMESTAMP_FMT)}{_FILE_SUFFIX}"


def _tmp_path_for(target: Path) -> Path:
    return target.with_name(target.name + _TMP_SUFFIX)


def _parse_backup_stamp(stamp: str) -> datetime | None:
    """Parse the timestamp part of a backup filename (both formats), UTC.

    Returns None for an unknown format — the caller must leave such files alone.
    """
    for fmt in (_TIMESTAMP_FMT, _LEGACY_TIMESTAMP_FMT):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def snapshot_copy(source: Path, dest: Path) -> None:
    """Consistent copy of `source` into `dest` via the SQLite backup API.

    `dest` must not be a path other code treats as a finished backup.
    The copy is switched to ``journal_mode=DELETE`` so it is a single
    self-contained file (a backup of a WAL database is otherwise WAL-flagged
    and would spawn ``-wal``/``-shm`` sidecars each time it is opened).
    """
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            try:
                dst.execute("PRAGMA journal_mode=DELETE").fetchone()
            except sqlite3.Error as exc:  # best-effort: the copy itself is fine
                logger.warning(f"Could not switch backup copy to journal_mode=DELETE: {exc}")
        finally:
            dst.close()
    finally:
        src.close()


def check_integrity(path: Path, log: logging.Logger | None = None) -> None:
    """Run ``PRAGMA integrity_check`` on `path`; raise BackupIntegrityError if not 'ok'.

    Plain sqlite3 connection on purpose (no sqlite-vec): virtual vec0 tables
    are skipped-over safely by integrity_check without the extension.
    The duration is logged (seconds) on `log` (default: this module's logger)
    so a slow check on a multi-GB DB is visible to the operator.
    """
    started = time.monotonic()
    conn = sqlite3.connect(str(path))
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError(f"integrity_check could not run on {path.name}: {exc}") from exc
    finally:
        conn.close()
    (log or logger).info(f"integrity_check duration: {time.monotonic() - started:.2f}s ({path.name})")
    if rows != ["ok"]:
        detail = "; ".join(str(r) for r in rows[:5])
        raise BackupIntegrityError(f"integrity_check failed on {path.name}: {detail}")


def is_backup_name(name: str) -> bool:
    """``plexhub-<stamp>.db`` with a parsable stamp (the files the pruner manages)."""
    if not (name.startswith(_FILE_PREFIX) and name.endswith(_FILE_SUFFIX)):
        return False
    return _parse_backup_stamp(name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]) is not None


def verified_sidecar_path(db_file: Path) -> Path:
    return db_file.with_name(db_file.name + _VERIFIED_SUFFIX)


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def write_verified_sidecar(measured: Path, final_name_path: Path) -> None:
    """Record that `measured` (the file as it will be published) passed ``integrity_check``.

    `measured` is stat-ed AFTER the check connection was closed. The sidecar is only valid for
    a file whose name, size AND mtime still match: replacing or editing the file voids it.
    """
    st = measured.stat()
    _write_json_atomic(verified_sidecar_path(final_name_path), {
        "name": final_name_path.name,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "integrity": "ok",
        "verified_at": int(time.time() * 1000),
    })


def write_attempt_state(backup_dir: Path, outcome: str) -> None:
    """Persist the last backup attempt (``ok`` | ``failed``); never raises."""
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(backup_dir / STATE_FILE_NAME, {
            "at_ms": int(time.time() * 1000), "outcome": outcome,
        })
    except OSError as exc:
        logger.warning(f"Could not record the backup attempt state: {exc}")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"Could not remove {path.name}: {exc}")


def remove_tmp_files(tmp: Path) -> None:
    """Remove a ``.tmp`` copy and any SQLite sidecar it may have spawned."""
    for p in (tmp, *(tmp.with_name(tmp.name + sfx) for sfx in _SIDECAR_SUFFIXES)):
        _unlink_quiet(p)


def perform_backup(source: Path, target: Path, *, verify: bool = True) -> None:
    """Atomic online backup: write ``target.tmp``, verify, ``os.replace`` to `target`.

    `target` only ever appears fully written and (when `verify`) integrity-checked.
    Any failure removes the temporary copy and re-raises.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(target)
    try:
        snapshot_copy(source, tmp)
        if verify:
            check_integrity(tmp)
            logger.info(f"Backup integrity_check: ok ({target.name})")
            if is_backup_name(target.name):
                write_verified_sidecar(tmp, target)   # BEFORE the rename: a crash leaves an orphan sidecar
        os.replace(tmp, target)
    finally:
        # After a successful replace `tmp` no longer exists (sidecars neither);
        # on any failure/interrupt this guarantees no partial leftover.
        remove_tmp_files(tmp)
        if not target.exists():
            _unlink_quiet(verified_sidecar_path(target))


def _is_orphan_tmp_name(name: str) -> bool:
    """``plexhub-*.db.tmp`` or one of its SQLite sidecars (``.tmp-wal/-shm/-journal``)."""
    if not name.startswith(_FILE_PREFIX):
        return False
    base = _FILE_SUFFIX + _TMP_SUFFIX
    return name.endswith(base) or any(name.endswith(base + sfx) for sfx in _SIDECAR_SUFFIXES)


def _sweep_older_than(directory: Path, matches, max_age_seconds: float) -> list[Path]:
    deleted: list[Path] = []
    if not directory.exists():
        return deleted
    cutoff = time.time() - max_age_seconds
    for f in directory.iterdir():
        if not f.is_file() or not matches(f.name):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                deleted.append(f)
        except OSError as exc:
            logger.warning(f"Could not sweep orphan {f.name}: {exc}")
    return deleted


def sweep_orphan_tmp(backup_dir: Path, max_age_seconds: float = _ORPHAN_TMP_MAX_AGE_SECONDS) -> list[Path]:
    """Delete ``plexhub-*.db.tmp`` (and ``-wal/-shm/-journal`` sidecars) older than `max_age_seconds`.

    Orphans of hard-killed backups; `prune_old_backups` only matches ``.db`` so
    it would never see them. Also removes ``*.db.verified`` files whose backup is gone
    (lot B2). Returns the deleted paths.
    """
    deleted = _sweep_older_than(backup_dir, _is_orphan_tmp_name, max_age_seconds)
    deleted += _sweep_older_than(
        backup_dir,
        lambda name: name.startswith(_FILE_PREFIX)
        and name.endswith(_FILE_SUFFIX + _VERIFIED_SUFFIX)
        and not (backup_dir / name[: -len(_VERIFIED_SUFFIX)]).exists(),
        max_age_seconds,
    )
    return deleted


def sweep_orphan_restore_tmp(db_path: Path, max_age_seconds: float = _ORPHAN_TMP_MAX_AGE_SECONDS) -> list[Path]:
    """Delete an orphaned ``<db>.restore-tmp`` (+ sidecars) beside the live DB.

    Written by ``restore_db`` (same volume as the target); a hard kill during a
    restore would leave it behind forever. Returns the deleted paths.
    """
    stem = db_path.name + RESTORE_TMP_SUFFIX
    names = {stem, *(stem + sfx for sfx in _SIDECAR_SUFFIXES)}
    return _sweep_older_than(db_path.parent, names.__contains__, max_age_seconds)


def prune_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    """Delete backups older than `retention_days` based on filename timestamp.

    Understands both the legacy (second) and the current (microsecond) name
    formats. Returns the list of deleted paths (for logging/tests).
    """
    if retention_days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted: list[Path] = []
    if not backup_dir.exists():
        return deleted
    for f in backup_dir.iterdir():
        if not f.is_file() or not f.name.startswith(_FILE_PREFIX) or not f.name.endswith(_FILE_SUFFIX):
            continue
        stamp = f.name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]
        ts = _parse_backup_stamp(stamp)
        if ts is None:
            continue  # Unknown filename — leave it alone.
        if ts < cutoff:
            try:
                f.unlink(missing_ok=True)
            except OSError as exc:  # a valid fresh backup must not fail on a stuck old one
                logger.warning(f"Could not prune old backup {f.name}: {exc}")
                continue
            _unlink_quiet(verified_sidecar_path(f))
            deleted.append(f)
    return deleted


def run_backup(retention_days: int | None = None) -> Path:
    """Snapshot the live DB (atomic + verified) and prune old backups.

    Returns the new backup path. Pruning only runs after a valid backup.
    """
    if not settings.DB_PATH.exists():
        write_attempt_state(settings.BACKUP_DIR, "failed")
        raise FileNotFoundError(f"DB not found at {settings.DB_PATH}")

    swept = sweep_orphan_tmp(settings.BACKUP_DIR) + sweep_orphan_restore_tmp(settings.DB_PATH)
    if swept:
        logger.info(f"Swept {len(swept)} orphaned backup .tmp file(s)")

    target = _build_target_path(settings.BACKUP_DIR)
    try:
        perform_backup(settings.DB_PATH, target)
    except BaseException:
        write_attempt_state(settings.BACKUP_DIR, "failed")
        raise
    write_attempt_state(settings.BACKUP_DIR, "ok")
    size_mb = target.stat().st_size / (1024 * 1024)
    logger.info(f"DB backup written: {target.name} ({size_mb:.1f} MB, integrity ok)")

    days = retention_days if retention_days is not None else settings.BACKUP_RETENTION_DAYS
    deleted = prune_old_backups(settings.BACKUP_DIR, days)
    if deleted:
        logger.info(f"Pruned {len(deleted)} old backup(s) (> {days}d)")
    return target


def verify_latest(backup_dir: Path) -> str | None:
    """Run a REAL ``integrity_check`` on the newest backup and record its sidecar.

    For a backup made before sidecars existed (lot B2). Returns the file name, or None when there
    is no backup. Raises ``BackupIntegrityError`` (and writes nothing) on a corrupt file.
    """
    if not backup_dir.exists():
        return None
    candidates = [
        f for f in backup_dir.iterdir()
        if f.is_file() and is_backup_name(f.name) and f.stat().st_size > 0
    ]
    if not candidates:
        return None
    newest = max(
        candidates,
        key=lambda f: _parse_backup_stamp(f.name[len(_FILE_PREFIX):-len(_FILE_SUFFIX)]),
    )
    check_integrity(newest)
    write_verified_sidecar(newest, newest)
    logger.info(f"Verified existing backup: {newest.name}")
    return newest.name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retention-days", type=int, default=None,
        help=f"Override BACKUP_RETENTION_DAYS (default: {settings.BACKUP_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--verify-latest", action="store_true",
        help="Run a real integrity_check on the newest existing backup and record its .verified "
             "sidecar (for a backup made before the metadata API existed); takes no new backup",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verify_latest:
        name = verify_latest(settings.BACKUP_DIR)
        print(f"verified: {name}" if name else "no backup found")
        return
    run_backup(args.retention_days)


if __name__ == "__main__":
    main()
