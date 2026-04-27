"""Online backup of plexhub.db using SQLite's native backup API.

Runs while the app is live (WAL mode allows concurrent readers/writers).
Output: BACKUP_DIR/plexhub-YYYYMMDD-HHMMSS.db, with retention pruning.

Usage (manual):
    python -m app.scripts.backup_db [--retention-days N]
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger("plexhub.scripts.backup")

_FILE_PREFIX = "plexhub-"
_FILE_SUFFIX = ".db"
_TIMESTAMP_FMT = "%Y%m%d-%H%M%S"


def _build_target_path(backup_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    return backup_dir / f"{_FILE_PREFIX}{now.strftime(_TIMESTAMP_FMT)}{_FILE_SUFFIX}"


def perform_backup(source: Path, target: Path) -> None:
    """Online backup via sqlite3.Connection.backup (works under WAL writes)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def prune_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    """Delete backups older than `retention_days` based on filename timestamp.

    Returns the list of deleted paths (for logging/tests).
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
        try:
            ts = datetime.strptime(stamp, _TIMESTAMP_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # Unknown filename — leave it alone.
        if ts < cutoff:
            f.unlink(missing_ok=True)
            deleted.append(f)
    return deleted


def run_backup(retention_days: int | None = None) -> Path:
    """Snapshot the live DB and prune old backups. Returns the new backup path."""
    if not settings.DB_PATH.exists():
        raise FileNotFoundError(f"DB not found at {settings.DB_PATH}")
    target = _build_target_path(settings.BACKUP_DIR)
    perform_backup(settings.DB_PATH, target)
    size_mb = target.stat().st_size / (1024 * 1024)
    logger.info(f"DB backup written: {target.name} ({size_mb:.1f} MB)")

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
