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

Usage (manual):
    python -m app.scripts.backup_db [--retention-days N]
Restore: see ``app/scripts/restore_db.py`` and ``docs/ops-backup-restore.md``.
"""
from __future__ import annotations

import argparse
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
# Legacy name format (second granularity) — still parsed by the pruner so
# backups written before the microsecond suffix existed keep being rotated.
_LEGACY_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"
# Current format: microseconds avoid two backups of the same second colliding.
_TIMESTAMP_FMT = "%Y%m%d-%H%M%S-%f"
# A ``.tmp`` younger than this may belong to a backup still running.
_ORPHAN_TMP_MAX_AGE_SECONDS = 3600


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


def check_integrity(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on `path`; raise BackupIntegrityError if not 'ok'.

    Plain sqlite3 connection on purpose (no sqlite-vec): virtual vec0 tables
    are skipped-over safely by integrity_check without the extension.
    """
    conn = sqlite3.connect(str(path))
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        raise BackupIntegrityError(f"integrity_check could not run on {path.name}: {exc}") from exc
    finally:
        conn.close()
    if rows != ["ok"]:
        detail = "; ".join(str(r) for r in rows[:5])
        raise BackupIntegrityError(f"integrity_check failed on {path.name}: {detail}")


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(f"Could not remove {path.name}: {exc}")


def remove_tmp_files(tmp: Path) -> None:
    """Remove a ``.tmp`` copy and any SQLite sidecar it may have spawned."""
    for p in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm"),
              tmp.with_name(tmp.name + "-journal")):
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
        os.replace(tmp, target)
    finally:
        # After a successful replace `tmp` no longer exists (sidecars neither);
        # on any failure/interrupt this guarantees no partial leftover.
        remove_tmp_files(tmp)


def sweep_orphan_tmp(backup_dir: Path, max_age_seconds: float = _ORPHAN_TMP_MAX_AGE_SECONDS) -> list[Path]:
    """Delete ``plexhub-*.db.tmp`` older than `max_age_seconds` (hard-killed backups).

    `prune_old_backups` only matches ``.db`` so it would never see them.
    Returns the deleted paths.
    """
    deleted: list[Path] = []
    if not backup_dir.exists():
        return deleted
    cutoff = time.time() - max_age_seconds
    for f in backup_dir.iterdir():
        if not f.is_file() or not f.name.startswith(_FILE_PREFIX) or not f.name.endswith(_FILE_SUFFIX + _TMP_SUFFIX):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                deleted.append(f)
        except OSError as exc:
            logger.warning(f"Could not sweep orphan {f.name}: {exc}")
    return deleted


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
            f.unlink(missing_ok=True)
            deleted.append(f)
    return deleted


def run_backup(retention_days: int | None = None) -> Path:
    """Snapshot the live DB (atomic + verified) and prune old backups.

    Returns the new backup path. Pruning only runs after a valid backup.
    """
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(f"DB not found at {settings.DB_PATH}")

    swept = sweep_orphan_tmp(settings.BACKUP_DIR)
    if swept:
        logger.info(f"Swept {len(swept)} orphaned backup .tmp file(s)")

    target = _build_target_path(settings.BACKUP_DIR)
    perform_backup(settings.DB_PATH, target)
    size_mb = target.stat().st_size / (1024 * 1024)
    logger.info(f"DB backup written: {target.name} ({size_mb:.1f} MB, integrity ok)")

    days = retention_days if retention_days is not None else settings.BACKUP_RETENTION_DAYS
    deleted = prune_old_backups(settings.BACKUP_DIR, days)
    if deleted:
        logger.info(f"Pruned {len(deleted)} old backup(s) (> {days}d)")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retention-days", type=int, default=None,
        help=f"Override BACKUP_RETENTION_DAYS (default: {settings.BACKUP_RETENTION_DAYS})",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_backup(args.retention_days)


if __name__ == "__main__":
    main()
