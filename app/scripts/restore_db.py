"""Restore plexhub.db from a backup file (FR-BE-874, Lot 10 sous-lot A).

DRY-RUN by default: reports what would happen and writes nothing.
``--apply`` performs the restore. Runbook: ``docs/ops-backup-restore.md``.

Usage:
    python -m app.scripts.restore_db BACKUP_FILE            # dry-run
    python -m app.scripts.restore_db BACKUP_FILE --apply    # restore

Safety sequence (``--apply``):
 1. refuse if the application holds the master lock
    (``DATA_DIR/server_start.lock``, non-blocking ``fcntl.flock``) — STOP THE
    APP FIRST;
 2. ``PRAGMA integrity_check`` on the source — a corrupt backup is refused;
 3. keep the current DB: dated copy ``plexhub.pre-restore-<ts>.db`` next to it
    (prefix ``plexhub.`` not ``plexhub-``: automatic pruning never touches it);
 4. copy the source to ``plexhub.db.restore-tmp`` beside the target and verify it;
 5. delete ``plexhub.db-wal`` / ``plexhub.db-shm`` — a stale WAL left next to a
    restored ``.db`` is replayed onto it and corrupts reads;
 6. ``os.replace`` the verified copy over ``plexhub.db`` (atomic).

Exit code: 0 on success (or clean dry-run), 1 on any refusal/failure.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.scripts import backup_db

logger = logging.getLogger("plexhub.scripts.restore")

_LOCK_NAME = "server_start.lock"
_RESTORE_TMP_SUFFIX = ".restore-tmp"


class RestoreError(RuntimeError):
    """A refusal or failure; the message is shown to the operator."""


class LockUnverifiable(RestoreError):
    """The master lock cannot be checked here (no ``fcntl``, e.g. Windows)."""


def _acquire_master_lock(lock_path: Path):
    """Take the app's master lock (non-blocking). Return the open handle.

    Raises RestoreError if the application (or another restore) holds it, and
    LockUnverifiable if ``fcntl`` is unavailable (Windows). Isolated in a
    function so tests can monkeypatch it.
    """
    try:
        import fcntl
    except ImportError as exc:
        raise LockUnverifiable(
            "fcntl indisponible sur cette plateforme (Windows ?) : impossible de "
            "vérifier que l'application est arrêtée. Lancer la restauration dans le "
            "conteneur Linux (voir docs/ops-backup-restore.md)."
        ) from exc
    fd = open(lock_path, "a")  # "a": never truncate the pid the app wrote there
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fd.close()
        raise RestoreError(
            f"L'application tient le verrou master ({lock_path}). "
            "Arrêter l'application avant de restaurer (docker compose stop)."
        ) from exc
    return fd


def _release_lock(handle) -> None:
    if handle is not None:
        handle.close()  # closing the fd releases the flock


def _sidecars(db_path: Path) -> list[Path]:
    return [db_path.with_name(db_path.name + "-wal"), db_path.with_name(db_path.name + "-shm")]


def _pre_restore_path(db_path: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    stem = db_path.stem  # "plexhub"
    return db_path.with_name(f"{stem}.pre-restore-{now.strftime('%Y%m%d-%H%M%S-%f')}{db_path.suffix}")


def _save_current_db(db_path: Path) -> Path:
    """Keep the current DB before overwriting it. Never blocks recovery.

    Normal path: consistent snapshot via the backup API (includes the WAL),
    integrity NOT required (the DB may be the very reason for the restore).
    If the DB is too damaged for the backup API, fall back to a raw byte copy
    of the file (and its -wal, so it stays re-openable).
    """
    target = _pre_restore_path(db_path)
    try:
        backup_db.perform_backup(db_path, target, verify=False)
    except sqlite3.Error as exc:
        logger.warning(f"Snapshot of current DB failed ({exc}); raw copy fallback")
        shutil.copy2(db_path, target)
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, target.with_name(target.name + "-wal"))
    return target


def restore(source: Path, db_path: Path, *, apply: bool, lock_path: Path | None = None) -> list[str]:
    """Run (or simulate) the restore. Returns human-readable report lines.

    Raises RestoreError on any refusal/failure; the current DB is then untouched
    unless the failure happens after the final steps (see the runbook).
    """
    lines: list[str] = []
    lock_path = lock_path or db_path.parent / _LOCK_NAME

    if not source.is_file():
        raise RestoreError(f"Source introuvable : {source}")
    if db_path.exists() and source.resolve() == db_path.resolve():
        raise RestoreError("La source est la base courante elle-même.")

    # 1. Application must be stopped. Held for the whole operation.
    handle = None
    try:
        try:
            handle = _acquire_master_lock(lock_path)
            lines.append(f"Verrou master : libre ({lock_path.name}).")
        except LockUnverifiable as exc:
            if apply:
                raise
            # dry-run writes nothing, so "cannot verify" is only reported.
            lines.append(f"Verrou master : NON VÉRIFIABLE ({exc}) — dry-run poursuivi.")

        # 2. Source integrity.
        try:
            backup_db.check_integrity(source)
        except backup_db.BackupIntegrityError as exc:
            raise RestoreError(f"Source refusée, base corrompue : {exc}") from exc
        lines.append(f"Source : {source} — integrity_check ok.")

        have_current = db_path.exists()
        if have_current:
            lines.append(
                f"Base courante {db_path.name} conservée sous "
                f"{db_path.stem}.pre-restore-<horodatage>{db_path.suffix} avant remplacement."
            )
        else:
            lines.append(f"Pas de base courante à {db_path} : aucune copie préalable.")
        stale = [p.name for p in _sidecars(db_path) if p.exists()]
        lines.append(
            "Fichiers WAL/SHM à supprimer : " + (", ".join(stale) if stale else "aucun")
            + f" ({db_path.name}-wal, {db_path.name}-shm)."
        )

        if not apply:
            lines.append("DRY-RUN — rien n'a été modifié. Relancer avec --apply pour restaurer.")
            return lines

        # 3. Keep the current DB.
        if have_current:
            saved = _save_current_db(db_path)
            lines.append(f"Base courante sauvegardée : {saved}")

        # 4. Verified copy beside the target (same volume => atomic replace).
        tmp = db_path.with_name(db_path.name + _RESTORE_TMP_SUFFIX)
        try:
            try:
                backup_db.snapshot_copy(source, tmp)
                backup_db.check_integrity(tmp)
            except (sqlite3.Error, backup_db.BackupIntegrityError) as exc:
                raise RestoreError(f"Copie de la source impossible ou invalide : {exc}") from exc

            # 5. Stale WAL/SHM next to the restored .db would corrupt reads.
            for p in _sidecars(db_path):
                p.unlink(missing_ok=True)

            # 6. Atomic swap.
            os.replace(tmp, db_path)
        finally:
            backup_db.remove_tmp_files(tmp)
        lines.append(f"Restauration terminée : {db_path} <- {source.name}. Redémarrer l'application.")
        return lines
    finally:
        _release_lock(handle)


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Restore plexhub.db from a backup file. Dry-run by default.",
    )
    p.add_argument("source", type=Path, help="Backup file (e.g. data/backups/plexhub-<ts>.db)")
    p.add_argument(
        "--apply", action="store_true", default=False,
        help="Perform the restore (default: dry-run, writes nothing). The "
             "application MUST be stopped.",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        lines = restore(args.source, Path(settings.DB_PATH), apply=args.apply)
    except RestoreError as exc:
        print(f"ÉCHEC : {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"ÉCHEC : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
